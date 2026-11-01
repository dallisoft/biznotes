"""Phase 2: 안전한 frontmatter 폼, eval 정적 검사, 로컬 CLI."""

import json

import pytest

from biznotes_core.services.skills import report as rpt
from biznotes_core.services.skills.authoring import frontmatter_form, rewrite_frontmatter
from biznotes_core.services.skills.parser import parse_skill_md_detailed
from biznotes_core.services.skills.validator import SkillValidationError


def skill_md(frontmatter: str, body: str = "# 본문\n\n바이트 그대로 유지\n") -> str:
    return f"---\n{frontmatter}\n---\n{body}"


def warning_codes(report: dict) -> list[str]:
    return [item["code"] for item in report["policy"]["warnings"]]


def test_form_rewrites_only_frontmatter_and_preserves_body() -> None:
    source = "\ufeff" + skill_md(
        "name: before\ndescription: before description\nlicense: Apache-2.0",
        "\r\n# 본문\r\n\r\n이 줄은 유지됩니다.\r\n",
    )
    state = frontmatter_form(source)
    assert state.editable is True
    assert state.fields["name"] == "before"

    updated = rewrite_frontmatter(
        source,
        {**state.fields, "name": "after", "description": "after description"},
    )

    assert updated.startswith("\ufeff---\n")
    assert updated.endswith("\r\n# 본문\r\n\r\n이 줄은 유지됩니다.\r\n")
    parsed = parse_skill_md_detailed(updated)
    assert parsed.frontmatter["name"] == "after"
    assert parsed.frontmatter["description"] == "after description"


@pytest.mark.parametrize(
    ("frontmatter", "reason"),
    [
        ("name: tool\ndescription: d\nowner: platform", "unknown_frontmatter_keys"),
        ("# keep this note\nname: tool\ndescription: d", "frontmatter_comments"),
        ("name: tool\ndescription: Bob's tool # keep this note", "frontmatter_comments"),
        (
            'name: tool\ndescription: say "hello # keep this note"',
            "frontmatter_comments",
        ),
        ("name: tool\ndescription: | # keep this note\n  body", "frontmatter_comments"),
    ],
)
def test_unknown_keys_or_comments_close_the_form(frontmatter: str, reason: str) -> None:
    state = frontmatter_form(skill_md(frontmatter))
    assert state.editable is False
    assert state.reason == reason
    with pytest.raises(SkillValidationError) as exc:
        rewrite_frontmatter(skill_md(frontmatter), {"name": "tool", "description": "d"})
    assert exc.value.reason == "frontmatter_form_unavailable"


def test_hash_inside_a_quoted_value_does_not_close_the_form() -> None:
    state = frontmatter_form(skill_md('name: tool\ndescription: "Use #tag safely"'))
    assert state.editable is True


def test_form_rejects_db_field_lengths_before_rewriting_source() -> None:
    source = skill_md("name: tool\ndescription: d")
    with pytest.raises(SkillValidationError) as exc:
        rewrite_frontmatter(source, {"name": "x" * 121, "description": "d"})
    assert exc.value.reason == "frontmatter_name_too_long"


def test_invalid_evals_json_is_a_policy_warning_not_a_freeze_error() -> None:
    evaluation = rpt.evaluate_package(
        {
            "SKILL.md": skill_md("name: tool\ndescription: d").encode(),
            "evals/evals.json": b"{bad json",
        }
    )
    assert warning_codes(evaluation.report) == ["evals_invalid_json"]
    assert evaluation.report["policy"]["errors"] == []


def test_evals_schema_reports_mismatch_and_missing_input_file() -> None:
    evaluation = rpt.evaluate_package(
        {
            "SKILL.md": skill_md("name: tool\ndescription: d").encode(),
            "evals/evals.json": json.dumps(
                {
                    "skill_name": "other",
                    "evals": [
                        {
                            "id": 1,
                            "prompt": "p",
                            "expected_output": "o",
                            "files": ["references/missing.md"],
                        },
                        {"id": 1, "prompt": "", "expected_output": "o"},
                    ],
                }
            ).encode(),
        }
    )
    assert warning_codes(evaluation.report) == [
        "evals_skill_name",
        "evals_missing_file",
        "evals_duplicate_id",
        "evals_schema",
    ]


@pytest.mark.parametrize(
    "extra",
    [
        {"metadata": {}},
        {"allowed-tools": []},
        {"license": ""},
    ],
)
def test_empty_optional_form_values_are_dropped_not_written_as_flow_style(extra: dict) -> None:
    updated = rewrite_frontmatter(
        skill_md("name: tool\ndescription: d"), {"name": "tool", "description": "d", **extra}
    )
    assert "{}" not in updated and "[]" not in updated and "''" not in updated
    report = rpt.evaluate_package({"SKILL.md": updated.encode()}).report
    assert report["standard"]["profile"] == "standard_v1"
    assert report["standard"]["errors"] == []


def test_form_keeps_typed_looking_strings_as_strings() -> None:
    updated = rewrite_frontmatter(
        skill_md("name: tool\ndescription: d"),
        {
            "name": "tool",
            "description": "yes",
            "compatibility": "3.12",
            "metadata": {"version": "1.0"},
        },
    )
    parsed = parse_skill_md_detailed(updated)
    assert parsed.frontmatter["description"] == "yes"
    assert parsed.frontmatter["compatibility"] == "3.12"
    assert parsed.frontmatter["metadata"] == {"version": "1.0"}
    report = rpt.evaluate_package({"SKILL.md": updated.encode()}).report
    assert report["standard"]["errors"] == []


class _StreamingRequest:
    """`_draft_file_bytes`가 쓰는 Request 표면(headers·stream)만 흉내 낸다."""

    def __init__(self, body: bytes, content_type: str, chunk: int = 4096) -> None:
        self.headers = {"content-type": content_type}
        self._body = body
        self._chunk = chunk
        self.consumed = 0

    async def stream(self):
        for start in range(0, len(self._body), self._chunk):
            piece = self._body[start : start + self._chunk]
            self.consumed += len(piece)
            yield piece


async def test_draft_file_body_keeps_json_text_contract_and_raw_bytes() -> None:
    from biznotes_core.api.v1.skills import _draft_file_bytes

    text = await _draft_file_bytes(
        _StreamingRequest(json.dumps({"content": "안녕"}).encode(), "application/json")
    )
    assert text == "안녕".encode()
    # JSON처럼 보이는 바이트도 octet-stream이면 envelope로 해석하지 않는다.
    raw = b'{"content": "x"}\x00\xff'
    assert await _draft_file_bytes(_StreamingRequest(raw, "application/octet-stream")) == raw


async def test_draft_file_body_is_cut_off_at_the_file_limit(monkeypatch) -> None:
    from fastapi import HTTPException

    from biznotes_core.api.v1 import skills as skills_api

    class _Settings:
        skill_import_max_file_bytes = 10_000

    monkeypatch.setattr(skills_api, "get_settings", lambda: _Settings())
    request = _StreamingRequest(b"x" * 100_000, "application/octet-stream", chunk=1000)
    with pytest.raises(HTTPException) as exc:
        await skills_api._draft_file_bytes(request)
    assert exc.value.status_code == 413
    # 한도를 넘는 순간 끊는다 — 본문 전체를 메모리에 올리지 않는다.
    assert request.consumed <= 11_000


@pytest.mark.parametrize("closing", ["---  ", "---\t"])
def test_closing_delimiter_with_trailing_space_keeps_the_body(closing: str) -> None:
    source = f"---\nname: x\ndescription: y\n{closing}\nBody line one # hash\n\n---\nmore body\n"
    state = frontmatter_form(source)
    assert state.editable is True
    updated = rewrite_frontmatter(source, {"name": "x", "description": "z"})
    assert updated.endswith("\nBody line one # hash\n\n---\nmore body\n")
    assert parse_skill_md_detailed(updated).body == parse_skill_md_detailed(source).body


def test_nested_empty_values_the_form_did_not_touch_are_preserved() -> None:
    source = skill_md(
        'name: tool\ndescription: d\nmetadata:\n  note: ""\n  author: a\n'
        'allowed-tools:\n- ""\n- Bash'
    )
    state = frontmatter_form(source)
    assert state.editable is True
    parsed = parse_skill_md_detailed(rewrite_frontmatter(source, state.fields))
    assert parsed.frontmatter["metadata"] == {"note": "", "author": "a"}
    assert parsed.frontmatter["allowed-tools"] == ["", "Bash"]


def test_form_rejects_values_the_parser_would_read_differently() -> None:
    with pytest.raises(SkillValidationError) as exc:
        rewrite_frontmatter(
            skill_md("name: tool\ndescription: d"), {"name": "tool", "description": "a\u2028b"}
        )
    assert exc.value.reason == "form_roundtrip_mismatch"


def test_form_rejects_metadata_deeper_than_the_parser_limit() -> None:
    from biznotes_core.services.skills.parser import SkillParseError

    deep: dict = {"leaf": "v"}
    for _ in range(40):
        deep = {"k": deep}
    with pytest.raises(SkillParseError):
        rewrite_frontmatter(
            skill_md("name: tool\ndescription: d"),
            {"name": "tool", "description": "d", "metadata": deep},
        )

