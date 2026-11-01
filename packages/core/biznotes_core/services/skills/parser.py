"""SKILL.md frontmatter 파싱 (skill_plan.md §9.1, §5.1 · 표준 계획 D1).

표준 호환: frontmatter의 `name`/`description`만 필수.

**문자열 스칼라 로더(D1)**: 표준 oracle(`skills-ref`)은 `strictyaml`로 frontmatter를 읽어
모든 스칼라를 문자열로 받는다. 예전 `yaml.safe_load`는 `compatibility: 3.12`를 float로,
`description: 2026-01-01`을 date로 읽어 oracle과 판정이 갈렸다. 그래서 PyYAML
`BaseLoader`(스칼라를 해석하지 않는다) 위에 strictyaml이 거부하는 구문을 직접 막는다.
`BaseLoader` 단독으로는 strictyaml과 같지 않다(리뷰 R2-07) — 아래가 그 차이를 메운다.

| 구문 | 처리 |
| --- | --- |
| alias·anchor | 거부(`yaml_alias`). alias는 직렬화 때 지수적으로 전개된다(리뷰 `2-01`) |
| 중복 키 | 거부(`yaml_duplicate_key`). 검토자와 다른 클라이언트가 서로 다른 값을 보게 된다 |
| 태그(`!!int`, `!!python/object…`) | 거부(`yaml_tag`) |
| flow 매핑·시퀀스 | 파싱한다. 대신 `flow_style`을 세워 표준 오류로 보고한다(거부하지 않음) |
| 깊은 중첩 | `skill_yaml_max_depth` 초과·`RecursionError`는 거부(`yaml_too_deep`, #69) |
| BOM | 벗기고 읽는다. `had_bom`으로 이식성 경고를 낸다 |

자동 복구(따옴표 없는 콜론 값에 따옴표 붙이기)는 하지 않는다 — 저장된 원문과 다른
클라이언트가 읽는 값이 달라지기 때문이다. 대신 오류에 수정 힌트를 붙인다.
"""

from dataclasses import dataclass
from typing import Any

import structlog
import yaml
from yaml.events import AliasEvent, CollectionStartEvent, NodeEvent, ScalarEvent
from yaml.nodes import MappingNode, ScalarNode

from biznotes_core.config import get_settings

logger = structlog.get_logger()

# 거부 사유 코드 — 가져오기·검증 응답의 reason으로 그대로 나간다(D4).
PARSE_ERROR = "parse_error"
YAML_ALIAS = "yaml_alias"
YAML_DUPLICATE_KEY = "yaml_duplicate_key"
YAML_TAG = "yaml_tag"
YAML_TOO_DEEP = "yaml_too_deep"
MISSING_NAME = "missing_name"
MISSING_DESCRIPTION = "missing_description"

_QUOTE_HINT = "값에 콜론(:)·#·따옴표 같은 특수 문자가 있으면 값을 따옴표로 감싸세요"


class SkillParseError(ValueError):
    """SKILL.md 파싱/필수 필드 검증 실패. reason 코드로 분류한다."""

    def __init__(self, message: str, reason: str = PARSE_ERROR) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class ParsedSkillMd:
    """frontmatter 파싱 결과와 표준 판정·이식성 경고에 쓰는 사실."""

    frontmatter: dict[str, Any]
    body: str
    flow_style: bool
    had_bom: bool


class _StringScalarLoader(yaml.BaseLoader):
    """스칼라를 문자열로만 읽고 strictyaml이 거부하는 구문을 막는 로더.

    검사는 compose 단계에서 한다 — 전개·생성 **전에** 막아야 증폭과 깊은 재귀가
    일어나지 않는다.
    """

    def __init__(self, stream: str, max_depth: int) -> None:
        super().__init__(stream)
        self._max_depth = max_depth
        self._depth = 0
        self.flow_style = False

    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        event = self.peek_event()
        if isinstance(event, AliasEvent):
            logger.warning("skill.frontmatter.alias_rejected", anchor=event.anchor)
            raise SkillParseError(
                "SKILL.md frontmatter에 YAML alias(*anchor)를 쓸 수 없습니다 "
                f"(alias={event.anchor!r}). 표준 파서(strictyaml)도 alias를 지원하지 않습니다.",
                YAML_ALIAS,
            )
        if isinstance(event, NodeEvent) and event.anchor is not None:
            raise SkillParseError(
                "SKILL.md frontmatter에 YAML anchor(&name)를 쓸 수 없습니다 "
                f"(anchor={event.anchor!r}). 표준 파서(strictyaml)도 anchor를 지원하지 않습니다.",
                YAML_ALIAS,
            )
        if isinstance(event, ScalarEvent | CollectionStartEvent) and event.tag is not None:
            raise SkillParseError(
                f"SKILL.md frontmatter에 YAML 태그({event.tag})를 쓸 수 없습니다. "
                "값은 모두 문자열로 읽습니다.",
                YAML_TAG,
            )
        return super().compose_node(parent, index)

    def _enter_collection(self) -> None:
        if self.peek_event().flow_style:
            self.flow_style = True
        self._depth += 1
        if self._depth > self._max_depth:
            raise SkillParseError(
                f"frontmatter YAML 중첩이 너무 깊습니다(상한 {self._max_depth})",
                YAML_TOO_DEEP,
            )

    def compose_sequence_node(self, anchor):  # type: ignore[no-untyped-def]
        self._enter_collection()
        try:
            return super().compose_sequence_node(anchor)
        finally:
            self._depth -= 1

    def compose_mapping_node(self, anchor):  # type: ignore[no-untyped-def]
        self._enter_collection()
        try:
            node = super().compose_mapping_node(anchor)
        finally:
            self._depth -= 1
        _reject_duplicate_keys(node)
        return node


def _reject_duplicate_keys(node: MappingNode) -> None:
    seen: set[str] = set()
    for key_node, _ in node.value:
        if not isinstance(key_node, ScalarNode):
            continue  # 복합 키는 생성 단계가 "unhashable key"로 거부한다
        if key_node.value in seen:
            raise SkillParseError(
                f"frontmatter에 같은 키가 두 번 있습니다: {key_node.value!r}. "
                "클라이언트마다 다른 값을 읽게 되므로 하나만 남기세요.",
                YAML_DUPLICATE_KEY,
            )
        seen.add(key_node.value)


def _load_frontmatter(raw: str) -> tuple[Any, bool]:
    loader = _StringScalarLoader(raw, get_settings().skill_yaml_max_depth)
    try:
        return loader.get_single_data(), loader.flow_style
    finally:
        loader.dispose()


def parse_skill_md_detailed(content: str) -> ParsedSkillMd:
    """SKILL.md 본문을 frontmatter·본문·판정용 사실로 분리한다.

    frontmatter는 파일 맨 앞 `---` ... `---` 사이의 YAML. 없으면 SkillParseError.
    """
    had_bom = content.startswith("\ufeff")
    text = content.lstrip("\ufeff")
    if not text.startswith("---"):
        raise SkillParseError("SKILL.md에 YAML frontmatter(---)가 없습니다")

    # 첫 줄 --- 이후부터 닫는 --- 까지
    lines = text.splitlines()
    if lines[0].strip() != "---":
        raise SkillParseError("SKILL.md는 '---'로 시작해야 합니다")
    closing = None
    for i in range(1, len(lines)):
        # 컬럼 0의 `---`만 닫는 구분자다. 들여쓴 `---`까지 받으면 블록 스칼라 안의 `---`에서
        # frontmatter가 잘려 정상 SKILL.md가 `missing_name`으로 거부된다.
        if lines[i].rstrip() == "---":
            closing = i
            break
    if closing is None:
        raise SkillParseError("frontmatter 닫는 '---'를 찾을 수 없습니다")

    fm_raw = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :]).lstrip("\n")

    try:
        fm, flow_style = _load_frontmatter(fm_raw)
    except yaml.YAMLError as e:
        raise SkillParseError(f"frontmatter YAML 파싱 실패: {e} ({_QUOTE_HINT})") from e
    except RecursionError as e:
        # 깊이 상한이 먼저 막지만, 상한을 높게 잡은 배포에서도 500으로 새지 않게 한다(#69)
        raise SkillParseError("frontmatter YAML 중첩이 너무 깊습니다", YAML_TOO_DEEP) from e
    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        raise SkillParseError("frontmatter는 키-값 매핑이어야 합니다")
    return ParsedSkillMd(frontmatter=fm, body=body, flow_style=flow_style, had_bom=had_bom)


def parse_skill_md(content: str) -> tuple[dict[str, Any], str]:
    """SKILL.md 본문을 (frontmatter dict, body) 로 분리."""
    parsed = parse_skill_md_detailed(content)
    return parsed.frontmatter, parsed.body


def validate_frontmatter(fm: dict[str, Any]) -> tuple[str, str]:
    """필수 필드(name/description) 검증 후 (name, description) 반환.

    `description` 누락은 클라이언트 가이드도 skip하는 조건이고, `name` 누락은 이
    제품이 스킬을 식별하는 데 필요해서 거부한다(목표 1).
    """
    name = fm.get("name")
    description = fm.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillParseError("frontmatter에 'name'(문자열)이 필요합니다", MISSING_NAME)
    if not isinstance(description, str) or not description.strip():
        raise SkillParseError(
            "frontmatter에 'description'(문자열)이 필요합니다", MISSING_DESCRIPTION
        )
    return name.strip(), description.strip()
