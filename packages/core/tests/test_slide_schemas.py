"""json_validate의 slide_outline·slide_deck 스키마 (#337).

검증 함수와 `json_validate`는 실경로다. LLM 호출은 게이트웨이 스트림 경계만 대역을 쓴다.
"""

import json
import uuid
from typing import Any

import pytest

from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.nodes import validation
from biznotes_core.services.agents.validators import (
    _JSON_VALIDATE_DICT_SCHEMAS,
    _JSON_VALIDATE_SCHEMAS,
    _prune_slide_deck_images,
    _validate_slide_deck_shape,
    _validate_slide_outline_shape,
)
from biznotes_core.services.gateway import llm as gateway_llm
from biznotes_core.services.gateway.image import IMAGE_PROMPT_MAX_CHARS
from biznotes_core.services.skills.validator import validate_manifest

OUTLINE = {
    "title": "커피의 역사",
    "format": "detailed",
    "slides": [
        {"title": "기원", "summary": "에티오피아 전설"},
        {"title": "전파"},
    ],
}

DECK = {
    "title": "커피의 역사",
    "format": "presenter",
    "slides": [
        {"heading": "커피의 역사", "layout": "title", "subheading": "기원에서 오늘까지"},
        {"heading": "전파 경로", "layout": "bullets", "bullets": ["예멘", "유럽"], "notes": "n"},
        {"heading": "비교", "layout": "two_column", "columns": [{"title": "a"}, {"title": "b"}]},
        {"heading": "단계", "layout": "steps", "steps": ["수확", "건조"]},
        {"heading": "수치", "layout": "stats", "stats": [{"value": "1위", "label": "소비"}]},
        {"heading": "인용", "layout": "quote", "quote": {"text": "커피는 …"}},
        {"heading": "섹션", "layout": "section", "body": "본문"},
        {"heading": "끝", "layout": "closing"},
    ],
}

VALIDATORS = {
    "slide_outline": _validate_slide_outline_shape,
    "slide_deck": _validate_slide_deck_shape,
}
SLIDE_KEY = {"slide_outline": "title", "slide_deck": "heading"}


def _minimal(schema: str) -> dict[str, Any]:
    return {"title": "제목", "slides": [{SLIDE_KEY[schema]: "첫 장"}]}


def test_both_schemas_are_registered_without_normalization() -> None:
    assert _JSON_VALIDATE_SCHEMAS["slide_outline"] is _validate_slide_outline_shape
    assert _JSON_VALIDATE_SCHEMAS["slide_deck"] is _validate_slide_deck_shape
    # 절단·truncated 메타는 마인드맵 전용이다 — 슬라이드 문서는 손대지 않는다
    assert not {"slide_outline", "slide_deck"} & _JSON_VALIDATE_DICT_SCHEMAS


# ── 수락 기준 1: 정상 문서·최소 문서 통과 ─────────────────────────────────


@pytest.mark.parametrize(
    ("schema", "doc"),
    [
        ("slide_outline", OUTLINE),
        ("slide_deck", DECK),
        ("slide_outline", _minimal("slide_outline")),
        ("slide_deck", _minimal("slide_deck")),
    ],
)
def test_normal_and_minimal_documents_pass(schema: str, doc: dict[str, Any]) -> None:
    assert VALIDATORS[schema](doc) is None


@pytest.mark.parametrize(
    "slide",
    [
        {"heading": "h", "layout": "timeline"},  # 모르는 layout
        {"heading": "h", "layout": 3},  # layout 타입이 틀려도 웹이 bullets로 그린다
        {"heading": "h", "layout": "stats"},  # 필수 필드가 빠진 layout
        {"heading": "h", "bullets": "문자열", "columns": 1, "steps": {}, "stats": None},
        {"heading": "h", "quote": 1, "notes": [], "body": 2, "subheading": False},
        {"heading": "h", "image": "x.png", "extra": {"a": 1}},  # 정의되지 않은 키
    ],
)
def test_deck_tolerates_unknown_layouts_and_bad_optional_fields(slide: dict[str, Any]) -> None:
    assert _validate_slide_deck_shape({"title": "t", "slides": [slide]}) is None


@pytest.mark.parametrize("schema", ["slide_outline", "slide_deck"])
def test_top_level_extras_and_odd_format_are_tolerated(schema: str) -> None:
    doc = {**_minimal(schema), "format": 1, "language": "ko", "version": 2}
    assert VALIDATORS[schema](doc) is None


def test_outline_tolerates_non_string_summary_and_extra_keys() -> None:
    doc = {"title": "t", "slides": [{"title": "a", "summary": 3, "points": ["x"]}]}
    assert _validate_slide_outline_shape(doc) is None


def test_validators_do_not_mutate_the_document() -> None:
    deck = json.loads(json.dumps(DECK))
    assert _validate_slide_deck_shape(deck) is None
    assert deck == DECK


# ── 수락 기준 2: 최소 형태 위반은 사유와 함께 거부 ─────────────────────────


@pytest.mark.parametrize("schema", ["slide_outline", "slide_deck"])
@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        ([{"title": "t"}], "최상위 값이 객체가 아닙니다"),
        ("문서", "최상위 값이 객체가 아닙니다"),
        (None, "최상위 값이 객체가 아닙니다"),
        ({"slides": [{}]}, "title이 비어있는 문자열입니다"),
        ({"title": "  ", "slides": [{}]}, "title이 비어있는 문자열입니다"),
        ({"title": 1, "slides": [{}]}, "title이 비어있는 문자열입니다"),
        ({"title": "t"}, "slides가 비어있지 않은 배열이 아닙니다"),
        ({"title": "t", "slides": []}, "slides가 비어있지 않은 배열이 아닙니다"),
        ({"title": "t", "slides": {"0": {}}}, "slides가 비어있지 않은 배열이 아닙니다"),
        ({"title": "t", "slides": ["첫 장"]}, "slides[0]가 객체가 아닙니다"),
    ],
)
def test_minimal_shape_violations_are_rejected(schema: str, doc: Any, reason: str) -> None:
    assert VALIDATORS[schema](doc) == reason


@pytest.mark.parametrize("schema", ["slide_outline", "slide_deck"])
@pytest.mark.parametrize("value", [None, "", "   ", 3, ["제목"]])
def test_empty_slide_title_or_heading_is_rejected(schema: str, value: Any) -> None:
    key = SLIDE_KEY[schema]
    slides = [{key: "첫 장"}, {} if value is None else {key: value}]

    assert VALIDATORS[schema]({"title": "t", "slides": slides}) == (
        f"slides[1].{key}이 비어있는 문자열입니다"
    )


def test_each_schema_checks_its_own_slide_key() -> None:
    # 개요는 slides[].title, 덱은 slides[].heading이다 — 서로의 키로는 통과하지 않는다
    assert _validate_slide_outline_shape({"title": "t", "slides": [{"heading": "h"}]}) == (
        "slides[0].title이 비어있는 문자열입니다"
    )
    assert _validate_slide_deck_shape({"title": "t", "slides": [{"title": "h"}]}) == (
        "slides[0].heading이 비어있는 문자열입니다"
    )


# ── 수락 기준 3·4: json_validate 실경로(펜스 → 재시도, 최종 실패) ─────────


def _ctx() -> RunContext:
    return RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """재시도 LLM 호출이 차례로 받을 응답 큐(게이트웨이 스트림 경계 대역)."""
    queue: list[str] = []

    async def stream(db: Any, model_id: Any, messages: Any, result: Any, **kw: Any) -> Any:
        reply = queue.pop(0)

        async def gen() -> Any:
            yield reply

        return gen()

    monkeypatch.setattr(gateway_llm, "complete_stream_with_fallback", stream)
    return queue


def _node(schema: str) -> dict[str, Any]:
    return {"id": "v", "type": "json_validate", "schema": schema, "source": "{gen.text}",
            "prompt": "슬라이드 JSON"}  # fmt: skip


@pytest.mark.parametrize(("schema", "doc"), [("slide_outline", OUTLINE), ("slide_deck", DECK)])
async def test_valid_output_passes_unchanged(
    replies: list[str], schema: str, doc: dict[str, Any]
) -> None:
    state = {"gen": {"text": json.dumps(doc, ensure_ascii=False)}}

    out = await validation._exec_json_validate(_ctx(), _node(schema), state)

    assert out["json"] == doc and "truncated" not in out["json"]
    assert json.loads(out["text"]) == doc
    assert replies == []  # 재시도 없음


@pytest.mark.parametrize(("schema", "doc"), [("slide_outline", OUTLINE), ("slide_deck", DECK)])
async def test_fenced_output_passes_through_the_retry_path(
    replies: list[str], schema: str, doc: dict[str, Any]
) -> None:
    fenced = "```json\n" + json.dumps(doc, ensure_ascii=False) + "\n```"
    replies.append(json.dumps(doc, ensure_ascii=False))

    out = await validation._exec_json_validate(_ctx(), _node(schema), {"gen": {"text": fenced}})

    assert out["json"] == doc
    assert replies == []  # 재시도 1회를 썼다


@pytest.mark.parametrize("schema", ["slide_outline", "slide_deck"])
async def test_shape_failure_after_one_retry_raises(replies: list[str], schema: str) -> None:
    replies.append(json.dumps({"title": "t", "slides": []}))
    state = {"gen": {"text": json.dumps({"title": "", "slides": [{}]})}}

    with pytest.raises(AgentRuntimeError, match=rf"최종 실패\({schema}\).*slides가 비어있지 않은"):
        await validation._exec_json_validate(_ctx(), _node(schema), state)

    assert replies == []


# ── 스킬 매니페스트도 같은 등록표를 쓴다 ──────────────────────────────────


@pytest.mark.parametrize("schema", ["slide_outline", "slide_deck"])
def test_skill_artifact_schema_accepts_the_slide_schemas(schema: str) -> None:
    artifact = {"name": f"{schema}.json", "content_type": "application/json", "schema": schema}

    out = validate_manifest({"artifact": artifact})

    assert out["artifact"]["schema"] == schema


# ── #345: slide_deck 선택 필드 images — 잘못된 항목만 버린다 ───────────────


def _deck_with(images: Any) -> dict[str, Any]:
    return {"title": "t", "slides": [{"heading": "a"}, {"heading": "b"}, {"heading": "c"}],
            "images": images}  # fmt: skip


@pytest.mark.parametrize("images", [None, []])
async def test_absent_null_or_empty_images_change_nothing(replies: list[str], images: Any) -> None:
    deck = _deck_with(images)
    if images is None:
        del deck["images"]

    out = await validation._exec_json_validate(
        _ctx(), _node("slide_deck"), {"gen": {"text": json.dumps(deck)}}
    )

    assert out["json"] == deck
    assert out["images_dropped"] == 0


async def test_valid_images_pass_unchanged_with_extra_keys(replies: list[str]) -> None:
    images = [
        {"slide": 3, "prompt": "커피 열매", "style": "watercolor"},
        {"slide": 1, "prompt": "p"},
    ]
    deck = _deck_with(images)

    out = await validation._exec_json_validate(
        _ctx(), _node("slide_deck"), {"gen": {"text": json.dumps(deck, ensure_ascii=False)}}
    )

    assert out["json"] == deck  # 순서·추가 키까지 그대로(정규화 없음)
    assert out["images_dropped"] == 0
    assert replies == []


async def test_issue_reproduction_keeps_only_the_valid_entry(replies: list[str]) -> None:
    """#345 재현 — 덱은 통과하고 잘못된 삽화 항목만 빠진다. 재시도하지 않는다."""
    images = [
        {"slide": "../x", "prompt": "p"},
        {"slide": 1.5, "prompt": "p"},
        {"slide": 2, "prompt": "둘째 장"},
        {"slide": 2, "prompt": "겹침"},
        {"slide": 99, "prompt": "p"},
        {"slide": 3},
        {"slide": 3, "prompt": "   "},
    ]
    deck = _deck_with(images)

    out = await validation._exec_json_validate(
        _ctx(), _node("slide_deck"), {"gen": {"text": json.dumps(deck, ensure_ascii=False)}}
    )

    assert out["json"]["images"] == [{"slide": 2, "prompt": "둘째 장"}]
    assert out["json"]["slides"] == deck["slides"]
    assert json.loads(out["text"])["images"] == [{"slide": 2, "prompt": "둘째 장"}]
    assert out["images_dropped"] == 6
    assert replies == []  # 삽화 위반은 재시도·실패 사유가 아니다


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ("문자열", "images[0]가 객체가 아닙니다"),
        ({"slide": "../x", "prompt": "p"}, "images[0].slide가 1~3 범위의 정수가 아닙니다"),
        ({"slide": "1", "prompt": "p"}, "images[0].slide가 1~3 범위의 정수가 아닙니다"),
        ({"slide": 1.5, "prompt": "p"}, "images[0].slide가 1~3 범위의 정수가 아닙니다"),
        ({"slide": 2.0, "prompt": "p"}, "images[0].slide가 1~3 범위의 정수가 아닙니다"),
        ({"slide": True, "prompt": "p"}, "images[0].slide가 1~3 범위의 정수가 아닙니다"),
        ({"slide": 0, "prompt": "p"}, "images[0].slide가 1~3 범위의 정수가 아닙니다"),
        ({"slide": 4, "prompt": "p"}, "images[0].slide가 1~3 범위의 정수가 아닙니다"),
        ({"prompt": "p"}, "images[0].slide가 1~3 범위의 정수가 아닙니다"),
        ({"slide": 1}, "images[0].prompt가 비어있는 문자열입니다"),
        ({"slide": 1, "prompt": ""}, "images[0].prompt가 비어있는 문자열입니다"),
        ({"slide": 1, "prompt": 3}, "images[0].prompt가 비어있는 문자열입니다"),
        ({"slide": 1, "prompt": "x" * 4001}, "images[0].prompt가 4000자를 넘습니다"),
    ],
)
def test_each_invalid_image_entry_is_dropped_with_its_location(item: Any, reason: str) -> None:
    deck = _deck_with([item])

    assert _prune_slide_deck_images(deck, IMAGE_PROMPT_MAX_CHARS) == [reason]
    assert deck["images"] == []


def test_prompt_at_the_length_limit_is_kept() -> None:
    deck = _deck_with([{"slide": 1, "prompt": "x" * IMAGE_PROMPT_MAX_CHARS}])

    assert _prune_slide_deck_images(deck, IMAGE_PROMPT_MAX_CHARS) == []
    assert len(deck["images"]) == 1


def test_duplicate_slide_keeps_the_first_valid_entry() -> None:
    # 앞의 잘못된 항목은 자리를 차지하지 않는다 — 같은 장의 다음 정상 항목이 남는다
    deck = _deck_with(
        [{"slide": 2}, {"slide": 2, "prompt": "첫 정상"}, {"slide": 2, "prompt": "겹침"}]
    )

    reasons = _prune_slide_deck_images(deck, IMAGE_PROMPT_MAX_CHARS)

    assert deck["images"] == [{"slide": 2, "prompt": "첫 정상"}]
    assert reasons == [
        "images[0].prompt가 비어있는 문자열입니다",
        "images[2].slide가 앞 항목과 겹칩니다: 2",
    ]


@pytest.mark.parametrize("images", [{"slide": 1, "prompt": "p"}, "삽화", 3, True])
def test_non_array_images_becomes_an_empty_array(images: Any) -> None:
    """배열이 아니면 비운다 — loop가 객체·문자열을 항목 1개로 감싸 본문을 부르지 않게."""
    deck = _deck_with(images)

    assert _prune_slide_deck_images(deck, IMAGE_PROMPT_MAX_CHARS) == ["images가 배열이 아닙니다"]
    assert deck["images"] == []


def test_images_are_ignored_by_the_shape_validator_and_outline() -> None:
    # 덱 형태 판정은 images와 무관하다 — 개요(slide_outline)는 images를 손대지 않는다
    bad = {"title": "t", "slides": [{"heading": "a"}], "images": "x"}
    assert _validate_slide_deck_shape(bad) is None
    assert _validate_slide_outline_shape({**bad, "slides": [{"title": "a"}]}) is None


async def test_outline_output_has_no_images_dropped(replies: list[str]) -> None:
    doc = {**OUTLINE, "images": [{"slide": 99}]}

    out = await validation._exec_json_validate(
        _ctx(), _node("slide_outline"), {"gen": {"text": json.dumps(doc, ensure_ascii=False)}}
    )

    assert out["json"] == doc and "images_dropped" not in out
