"""채팅 진입 판정 — workflow와 스킬 wrapper (#85, `docs/plans/skill-chat-entry/`, #410).

판정 규칙은 순수 함수(`_decide_*`·`_chat_param_key`)를 직접 부르거나, `db.get`만 쓰는 단건 경로
(`resolve_chat_entry`·`assert_chat_entry_allowed`)를 가짜 DB로 돌려 확인한다. `execute`를 쓰는 목록·
일괄 적재 쿼리와 승인 조회는 `tests/test_chat_entry_pg.py`가 PostgreSQL로 검증한다. 여기서는 승인
조회(`exposure.approved_exposure_version_ids`)만 가짜 DB의 승인 집합으로 바꾼다.
"""

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from biznotes_core.config import reset_settings
from biznotes_core.models.skill import SKILL_RUNTIME_LEGACY, SKILL_RUNTIME_STANDARD
from biznotes_core.services.agent_runs import _validate_against, resolve_wrapper_run_version
from biznotes_core.services.agents import chat_entry, policy
from biznotes_core.services.agents.chat_entry import (
    ChatEntry,
    ChatEntryReason,
    _chat_param_key,
    _decide_skill,
    _decide_workflow,
    assert_chat_entry_allowed,
    chat_entry_params,
    resolve_chat_entries,
    resolve_chat_entry,
)
from biznotes_core.services.authz import is_chat_callable
from biznotes_core.services.skills.runtime import params_schema_for_version

R = ChatEntryReason

OWNER = SimpleNamespace(id=uuid.uuid4(), role="user")
OTHER = SimpleNamespace(id=uuid.uuid4(), role="user")
ADMIN = SimpleNamespace(id=uuid.uuid4(), role="admin")

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {"topic": {"type": "string", "title": "주제"}},
    "required": ["topic"],
}
# 문자열 속성이 없다 — 채팅 메시지를 옮길 수 없는 legacy 스키마
NO_STRING = {"type": "object", "properties": {"n": {"type": "integer"}}}


class FakeDB:
    def __init__(self) -> None:
        self.objects: dict[Any, Any] = {}
        # approved `expose_*` 검토 요청이 있는 version id(#410 D1)
        self.approved: set[uuid.UUID] = set()

    async def get(self, _model: Any, key: Any) -> Any:
        return self.objects.get(key)

    def add(self, *objs: Any) -> None:
        for obj in objs:
            self.objects[obj.id] = obj


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


@pytest.fixture(autouse=True)
def _approvals_from_fake_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """승인 조회 SQL만 가짜 DB의 승인 집합으로 바꾼다 — SQL은 PG 테스트가 검증한다."""
    from biznotes_core.services.skills import exposure

    async def approved(db: Any, version_ids: Any) -> set[uuid.UUID]:
        return {vid for vid in version_ids if vid in db.approved}

    monkeypatch.setattr(exposure, "approved_exposure_version_ids", approved, raising=False)


@pytest.fixture
def standard_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true")
    reset_settings()
    yield
    monkeypatch.delenv("SKILL_STANDARD_RUNTIME_ENABLED")
    reset_settings()


# ---------- 가짜 행 ----------


def react_definition(**node: Any) -> dict[str, Any]:
    return {
        "nodes": [{"id": "a", "type": "react_agent", "allowed_tools": ["rag_search"], **node}],
        "edges": [],
    }


def workflow(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "wf",
        "kind": "workflow",
        "source_kind": "legacy",
        "scope": "system",
        "owner_id": None,
        "enabled": True,
        "definition": react_definition(),
        "skill_version_id": None,
        "handle": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def version(
    skill_id: uuid.UUID,
    *,
    profile: str = SKILL_RUNTIME_LEGACY,
    params_schema: Any = None,
    rag_query: str | None = None,
    name: str = "",
    description: str = "",
) -> SimpleNamespace:
    schema = TOPIC_SCHEMA if params_schema is None else params_schema
    manifest: dict[str, Any] = {"params_schema": schema}
    if rag_query is not None:
        manifest["rag_query"] = rag_query
    return SimpleNamespace(
        id=uuid.uuid4(),
        skill_id=skill_id,
        runtime_profile=profile,
        manifest={} if profile == SKILL_RUNTIME_STANDARD else manifest,
        standard_metadata={"name": name, "description": description} if name else {},
    )


class World:
    """스킬 하나와 그 wrapper — 기본은 타인 소유 system 스킬, 공개 version 하나(legacy)."""

    def __init__(self, db: FakeDB, *, owner: Any = OWNER, **skill_fields: Any) -> None:
        self.db = db
        skill_id = uuid.uuid4()
        self.published = version(skill_id, name="pdf-helper", description="PDF 작업")
        self.skill = SimpleNamespace(
            id=skill_id,
            owner_id=owner.id if owner is not None else None,
            name="PDF 도우미",
            description="스킬 목록 설명",
            slug="pdf-helper-slug",
            visibility="system",
            enabled=True,
            current_version_id=self.published.id,
            published_version_id=self.published.id,
        )
        for key, value in skill_fields.items():
            setattr(self.skill, key, value)
        self.wrapper = SimpleNamespace(
            id=uuid.uuid4(),
            name="wrapper-latest-name",
            description="wrapper-latest-description",
            kind="skill",
            source_kind="skill_package",
            scope="system",
            owner_id=self.skill.owner_id,
            enabled=True,
            definition={},
            params_schema={},
            skill_version_id=self.published.id,
        )
        db.add(self.skill, self.published)

    def freeze(self, **kw: Any) -> SimpleNamespace:
        """소유자의 새 freeze — wrapper가 최신 version을 가리킨다(`_upsert_wrapper`)."""
        new = version(self.skill.id, **kw)
        self.db.add(new)
        self.skill.current_version_id = new.id
        self.wrapper.skill_version_id = new.id
        return new

    def publish(self, target: SimpleNamespace) -> None:
        self.skill.published_version_id = target.id


def decide(
    user: Any, world: World, *, standard_enabled: bool = False, org_approved: bool = False
) -> ChatEntry:
    """단건 적재 없이 순수 판정 — 실행 규칙 version은 `resolve_run_version_id`로 고른다."""
    from biznotes_core.services.skills.exposure import resolve_run_version_id

    vid = resolve_run_version_id(world.skill, user.id)
    approval = {"org_approved": True} if org_approved else {}  # 기본값은 판정 함수의 것
    return _decide_skill(
        user,
        world.wrapper,
        world.skill,
        world.db.objects.get(vid),
        standard_enabled=standard_enabled,
        **approval,
    )


# ---------- workflow 경로 (A3) ----------


@pytest.mark.parametrize(
    "definition",
    [
        react_definition(),
        {
            "nodes": [
                {"id": "a", "type": "react_agent", "allowed_tools": ["rag_search"]},
                {"id": "b", "type": "llm", "prompt": "x"},
            ],
            "edges": [["a", "b"]],
        },
        react_definition(allowed_tools=["rag_search", "call_agent"]),
        {**react_definition(), "chat_output": {"node_id": "missing"}},
        {"nodes": [{"id": "a", "type": "llm", "prompt": "x"}], "edges": []},
    ],
    ids=["callable", "multi-node", "call-agent", "bad-chat-output", "llm-node"],
)
@pytest.mark.parametrize("scope_owner", ["system", "owner"])
def test_visible_enabled_workflow_matches_is_chat_callable(
    definition: dict[str, Any], scope_owner: str
) -> None:
    agent = (
        workflow(definition=definition)
        if scope_owner == "system"
        else workflow(definition=definition, scope="user", owner_id=OWNER.id)
    )
    entry = _decide_workflow(OWNER, agent)
    assert entry.allowed == is_chat_callable(agent)
    assert entry.reason == (R.OK if entry.allowed else R.WORKFLOW_SHAPE)
    assert entry.skill_version_id is None and entry.param_key is None
    assert entry.display_name is None and entry.display_description is None


def test_admin_does_not_see_another_users_private_workflow() -> None:
    agent = workflow(scope="user", owner_id=OWNER.id)
    assert _decide_workflow(ADMIN, agent).reason == R.NOT_VISIBLE
    assert _decide_workflow(OTHER, agent).reason == R.NOT_VISIBLE


def test_owner_draft_follows_is_chat_callable() -> None:
    draft = workflow(scope="user", owner_id=OWNER.id, enabled=False)
    assert _decide_workflow(OWNER, draft).allowed
    broken = workflow(scope="user", owner_id=OWNER.id, enabled=False, definition={"nodes": []})
    assert _decide_workflow(OWNER, broken).reason == R.WORKFLOW_SHAPE


def test_disabled_system_workflow_is_not_enabled_for_others() -> None:
    assert _decide_workflow(OTHER, workflow(enabled=False)).reason == R.NOT_ENABLED


def test_non_workflow_kind_is_not_chat_shape() -> None:
    assert _decide_workflow(OTHER, workflow(kind="tool")).reason == R.WORKFLOW_SHAPE


@pytest.mark.parametrize(
    ("user", "agent", "reason"),
    [
        (
            OTHER,
            workflow(scope="user", owner_id=OWNER.id, enabled=False, definition={"nodes": []}),
            R.NOT_VISIBLE,
        ),
        (ADMIN, workflow(scope="user", owner_id=OWNER.id, enabled=False), R.NOT_VISIBLE),
        (OTHER, workflow(enabled=False, definition={"nodes": []}), R.NOT_ENABLED),
    ],
    ids=["hidden-before-disabled", "admin-hidden-before-disabled", "disabled-before-shape"],
)
def test_workflow_reports_the_first_failing_condition(user: Any, agent: Any, reason: R) -> None:
    """조건이 겹치면 앞 단계(D2 workflow 1→2→3)의 사유를 낸다."""
    assert _decide_workflow(user, agent).reason == reason


# ---------- 스킬 허용 (A1·A5) ----------


async def test_non_owner_runs_the_published_legacy_version(db: FakeDB) -> None:
    world = World(db)
    entry = await resolve_chat_entry(db, OTHER, world.wrapper)
    assert entry == ChatEntry(
        allowed=True,
        reason=R.OK,
        skill_version_id=world.published.id,
        param_key="topic",
        display_name="pdf-helper",
        display_description="PDF 작업",
    )
    _, run_version = await resolve_wrapper_run_version(db, world.wrapper, OTHER.id)
    assert entry.skill_version_id == run_version.id


async def test_standard_version_maps_to_request(db: FakeDB, standard_on: None) -> None:
    world = World(db)
    standard = world.freeze(profile=SKILL_RUNTIME_STANDARD, name="pdf-std")
    world.publish(standard)
    entry = await resolve_chat_entry(db, OTHER, world.wrapper)
    assert entry.allowed and entry.param_key == "request"
    assert entry.skill_version_id == standard.id
    _, run_version = await resolve_wrapper_run_version(db, world.wrapper, OTHER.id)
    assert entry.skill_version_id == run_version.id


async def test_system_skill_owner_runs_the_latest_freeze(db: FakeDB) -> None:
    """B1 — 소유자는 공개 version이 아니라 최신 freeze를 실행하고, 판정도 그 version이다."""
    world = World(db)
    latest = world.freeze(
        params_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        rag_query="{params.q}",
        name="pdf-next",
    )
    assert world.skill.current_version_id != world.skill.published_version_id
    owner_entry = await resolve_chat_entry(db, OWNER, world.wrapper)
    assert owner_entry.allowed
    assert owner_entry.skill_version_id == latest.id
    assert owner_entry.param_key == "q"
    assert owner_entry.display_name == "pdf-next"
    _, owner_run = await resolve_wrapper_run_version(db, world.wrapper, OWNER.id)
    assert owner_entry.skill_version_id == owner_run.id

    for user in (OTHER, ADMIN):  # admin도 비소유자다 — 미공개 freeze를 보지 않는다
        entry = await resolve_chat_entry(db, user, world.wrapper)
        assert (entry.skill_version_id, entry.display_name) == (world.published.id, "pdf-helper")
        _, run = await resolve_wrapper_run_version(db, world.wrapper, user.id)
        assert entry.skill_version_id == run.id


async def test_hand_made_workflow_kind_wrapper_takes_the_skill_path(db: FakeDB) -> None:
    """`kind="workflow"`로 만든 wrapper도 `source_kind`로 스킬 경로를 탄다."""
    world = World(db)
    world.wrapper.kind = "workflow"
    world.wrapper.definition = react_definition()
    assert is_chat_callable(world.wrapper)  # workflow 규칙이면 통과했을 정의다
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).param_key == "topic"
    world.skill.enabled = False
    assert (await resolve_chat_entry(db, OWNER, world.wrapper)).reason == R.SKILL_DISABLED
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).reason == R.NOT_VISIBLE


# ---------- 스킬 거부 (A1·A2) ----------


def test_org_skill_needs_an_approved_exposed_version_for_non_owners(db: FakeDB) -> None:
    """#410 D1 — 비소유자(관리자 포함)는 노출 version이 승인됐을 때만 허용한다.

    소유자는 승인과 무관하게 최신 freeze로 허용한다.
    """
    world = World(db, visibility="org")
    latest = world.freeze(name="org-next")
    for user in (OTHER, ADMIN):
        denied = decide(user, world)
        assert (denied.allowed, denied.reason) == (False, R.SKILL_NOT_PUBLISHED)
        assert denied.display_name == "pdf-helper"  # 볼 수 있는 org 행 — 노출 version 표시
        assert (denied.slash_name, denied.source) == (None, None)
        entry = decide(user, world, org_approved=True)
        assert (entry.allowed, entry.reason) == (True, R.OK)
        assert entry.skill_version_id == world.published.id
        assert (entry.slash_name, entry.source) == ("pdf-helper-slug", "org")
    owner = decide(OWNER, world)  # 소유자는 승인·포인터와 무관하다
    assert (owner.allowed, owner.skill_version_id, owner.source) == (True, latest.id, "own")
    assert owner.slash_name == "pdf-helper-slug"


def test_disabled_skill_is_hidden_from_others_and_explained_to_the_owner(db: FakeDB) -> None:
    world = World(db, enabled=False)
    assert decide(OWNER, world).reason == R.SKILL_DISABLED
    hidden = decide(OTHER, world)
    assert hidden == ChatEntry(allowed=False, reason=R.NOT_VISIBLE)


def test_disabled_wrapper_is_skill_disabled(db: FakeDB) -> None:
    world = World(db)
    world.wrapper.enabled = False
    assert decide(OTHER, world).reason == R.SKILL_DISABLED


def test_unpublished_system_skill_is_not_published_even_for_the_owner(db: FakeDB) -> None:
    world = World(db, published_version_id=None)
    assert decide(OTHER, world).reason == R.SKILL_NOT_PUBLISHED
    assert decide(OWNER, world).reason == R.SKILL_NOT_PUBLISHED


async def test_wrapper_without_version_is_not_visible(db: FakeDB) -> None:
    world = World(db)
    world.wrapper.skill_version_id = None
    assert await resolve_chat_entry(db, OWNER, world.wrapper) == ChatEntry(
        allowed=False, reason=R.NOT_VISIBLE
    )


async def test_standard_version_with_the_flag_off_is_runtime_disabled(db: FakeDB) -> None:
    world = World(db)
    world.publish(world.freeze(profile=SKILL_RUNTIME_STANDARD))
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).reason == R.SKILL_RUNTIME_DISABLED


async def test_runtime_flag_is_judged_on_the_version_that_will_run(db: FakeDB) -> None:
    """B1 — 공개 version은 legacy여도 소유자의 최신 freeze가 standard면 소유자만 막힌다."""
    world = World(db)
    world.freeze(profile=SKILL_RUNTIME_STANDARD)
    assert (await resolve_chat_entry(db, OWNER, world.wrapper)).reason == R.SKILL_RUNTIME_DISABLED
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).allowed


async def test_params_rule_is_judged_on_the_version_that_will_run(db: FakeDB) -> None:
    """B1 — 파라미터 규칙도 실행될 version으로 판정한다. 어느 쪽이 비호환이든 그 actor만 막힌다."""
    world = World(db)
    world.freeze(params_schema=NO_STRING)
    assert (await resolve_chat_entry(db, OWNER, world.wrapper)).reason == (
        R.SKILL_PARAMS_INCOMPATIBLE
    )
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).param_key == "topic"

    world = World(db)
    world.published.manifest = {"params_schema": NO_STRING}
    world.freeze()
    assert (await resolve_chat_entry(db, OWNER, world.wrapper)).param_key == "topic"
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).reason == (
        R.SKILL_PARAMS_INCOMPATIBLE
    )


def test_skill_reports_the_first_failing_condition(db: FakeDB) -> None:
    """조건이 겹치면 앞 단계(D2 스킬 3→4→5→7·8)의 사유를 낸다. 소유자는 세분 사유를 본다."""
    world = World(db, enabled=False, visibility="org", published_version_id=None)
    assert decide(OWNER, world).reason == R.SKILL_DISABLED  # 3 > 4·5

    world = World(db, visibility="org")  # enabled org는 비소유자도 볼 수 있다
    world.wrapper.enabled = False
    assert decide(OTHER, world).reason == R.SKILL_DISABLED  # 3 > 4

    world = World(db, published_version_id=None)
    world.freeze(profile=SKILL_RUNTIME_STANDARD)
    assert decide(OWNER, world).reason == R.SKILL_NOT_PUBLISHED  # 5 > 7

    world = World(db, published_version_id=None)
    world.freeze(params_schema=NO_STRING)
    assert decide(OWNER, world).reason == R.SKILL_NOT_PUBLISHED  # 5 > 8


def test_incompatible_params_are_excluded(db: FakeDB) -> None:
    world = World(db)
    world.published.manifest = {
        "params_schema": {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a", "b"],
        }
    }
    entry = decide(OTHER, world)
    assert entry.reason == R.SKILL_PARAMS_INCOMPATIBLE
    assert entry.display_name == "pdf-helper"  # 볼 수 있는 행에는 표시 필드를 준다


def test_owner_private_skill_is_allowed_and_hidden_from_others(db: FakeDB) -> None:
    """#410 — 소유자는 공개 포인터 없이 최신 freeze로 허용, 비소유자(관리자 포함)는 존재 은닉."""
    world = World(db, visibility="private", published_version_id=None)
    entry = decide(OWNER, world)
    assert (entry.allowed, entry.reason, entry.skill_version_id) == (
        True,
        R.OK,
        world.published.id,
    )
    assert (entry.slash_name, entry.source, entry.param_key) == ("pdf-helper-slug", "own", "topic")
    for user in (OTHER, ADMIN):
        hidden = decide(user, world, org_approved=True)
        assert hidden == ChatEntry(allowed=False, reason=R.NOT_VISIBLE)
        assert (hidden.slash_name, hidden.source, hidden.display_name) == (None, None, None)


def test_run_version_of_another_skill_is_rejected(db: FakeDB) -> None:
    world = World(db)
    stray = version(uuid.uuid4())
    entry = _decide_skill(OTHER, world.wrapper, world.skill, stray, standard_enabled=True)
    assert entry.reason == R.SKILL_NOT_PUBLISHED
    assert entry.display_name == "PDF 도우미"  # 다른 스킬의 frontmatter를 쓰지 않는다


# ---------- 표시 필드 (N2) ----------


async def test_display_comes_from_the_run_version_not_the_wrapper(db: FakeDB) -> None:
    world = World(db)
    world.freeze(name="unpublished-name", description="미공개 설명")
    entry = await resolve_chat_entry(db, OTHER, world.wrapper)
    assert (entry.display_name, entry.display_description) == ("pdf-helper", "PDF 작업")
    for leaked in ("unpublished-name", "미공개 설명", world.wrapper.name):
        assert leaked not in repr(entry)


async def test_display_falls_back_to_the_skill_row(db: FakeDB) -> None:
    world = World(db)
    world.published.standard_metadata = {}
    entry = await resolve_chat_entry(db, OTHER, world.wrapper)
    assert (entry.display_name, entry.display_description) == ("PDF 도우미", "스킬 목록 설명")


# ---------- 파라미터 규칙 (B2·N3) ----------


def _legacy(schema: Any) -> SimpleNamespace:
    # 형태 규칙만 본다 — RAG 검색어 규칙(#115)은 검색 노드가 없는 매니페스트로 비켜 간다
    return SimpleNamespace(
        runtime_profile=SKILL_RUNTIME_LEGACY,
        manifest={"params_schema": schema, "input": {"mode": "none"}},
    )


def _obj(properties: dict[str, Any], required: list[Any] | None = None, **root: Any) -> Any:
    schema: dict[str, Any] = {"type": "object", "properties": properties, **root}
    if required is not None:
        schema["required"] = required
    return schema


S = {"type": "string"}

ACCEPTED = {
    "required-string": (_obj({"topic": S}, ["topic"]), "topic"),
    "optional-single-string": (
        _obj({"topic": S, "limit": {"type": "integer"}, "flag": {"type": "boolean"}}),
        "topic",
    ),
    "no-root-type": ({"properties": {"q": S}, "required": ["q"]}, "q"),
    "closed-object": (_obj({"q": S}, ["q"], additionalProperties=False), "q"),
    "annotated-target": (
        _obj(
            {"q": {**S, "title": "질문", "description": "d", "default": "", "examples": ["x"]}},
            ["q"],
            title="t",
            description="d",
            **{"$schema": "https://json-schema.org/draft/2020-12/schema", "$comment": "c"},
        ),
        "q",
    ),
    "required-string-with-other-optional-string": (_obj({"q": S, "r": S}, ["q"]), "q"),
    # 비어 있지 않음만 요구하는 제약은 채팅 메시지가 항상 만족한다(#114)
    "minLength-0": (_obj({"q": {**S, "minLength": 0}}, ["q"]), "q"),
    "minLength-1": (_obj({"q": {**S, "minLength": 1}}, ["q"]), "q"),
    "optional-minLength-1": (_obj({"q": {**S, "minLength": 1, "title": "주제"}}), "q"),
    "explicit-empty-required": (_obj({"q": S}, []), "q"),
    # 제약이 있는 문자열은 대상 조건을 만족하지 않는다 — 조건을 만족하는 속성은 q 하나다
    "optional-string-beside-constrained-string": (
        _obj({"q": S, "lang": {**S, "enum": ["ko", "en"]}}),
        "q",
    ),
}

REJECTED = {
    "two-required": _obj({"a": S, "b": S}, ["a", "b"]),
    "required-number": _obj({"n": {"type": "number"}}, ["n"]),
    "no-string": _obj({"n": {"type": "integer"}}),
    "two-optional-strings": _obj({"a": S, "b": S}),
    "required-missing": _obj({"a": S}, ["b"]),
    "required-not-list": _obj({"a": S}, "a"),  # type: ignore[arg-type]
    "required-non-string": _obj({"a": S}, [1]),
    "enum": _obj({"a": {**S, "enum": ["x"]}}, ["a"]),
    "const": _obj({"a": {**S, "const": "x"}}, ["a"]),
    "pattern": _obj({"a": {**S, "pattern": "^x"}}, ["a"]),
    "minLength": _obj({"a": {**S, "minLength": 3}}, ["a"]),
    "minLength-2": _obj({"a": {**S, "minLength": 2}}, ["a"]),
    # 필수가 없으면 minLength 1 속성도 후보라 후보가 둘이 된다(#114 전에는 r 하나를 골랐다)
    "optional-minLength-beside-plain-string": _obj({"q": {**S, "minLength": 1}, "r": S}),
    "minLength-1-with-maxLength": _obj({"a": {**S, "minLength": 1, "maxLength": 3}}, ["a"]),
    "minLength-1-with-enum": _obj({"a": {**S, "minLength": 1, "enum": ["x"]}}, ["a"]),
    "minLength-float": _obj({"a": {**S, "minLength": 1.0}}, ["a"]),
    "minLength-bool": _obj({"a": {**S, "minLength": True}}, ["a"]),
    "minLength-negative": _obj({"a": {**S, "minLength": -1}}, ["a"]),
    "minLength-string": _obj({"a": {**S, "minLength": "1"}}, ["a"]),
    "maxLength": _obj({"a": {**S, "maxLength": 3}}, ["a"]),
    "format": _obj({"a": {**S, "format": "email"}}, ["a"]),
    "nullable-type-list": _obj({"a": {"type": ["string", "null"]}}, ["a"]),
    "untyped-target": _obj({"a": {"title": "a"}}, ["a"]),
    "boolean-subschema-target": _obj({"a": True}, ["a"]),
    "allOf": _obj({"a": S}, ["a"], allOf=[{}]),
    "anyOf": _obj({"a": S}, ["a"], anyOf=[{}]),
    "oneOf": _obj({"a": S}, ["a"], oneOf=[{}]),
    "if": _obj({"a": S}, ["a"], **{"if": {}}),
    "$ref": _obj({"a": S}, ["a"], **{"$ref": "#/$defs/x"}),
    "minProperties": _obj({"a": S}, ["a"], minProperties=2),
    "dependentRequired": _obj({"a": S}, ["a"], dependentRequired={"a": ["b"]}),
    "root-not-object": {"type": "string"},
    "root-array": {"type": "array", "properties": {"a": S}, "required": ["a"]},
    "root-type-list": {"type": ["object"], "properties": {"a": S}, "required": ["a"]},
    "properties-not-dict": {"type": "object", "properties": [], "required": []},
    "no-properties": {"type": "object"},
    "empty": {},
    "boolean-true": True,
    "boolean-false": False,
    "not-a-dict": "object",
}


@pytest.mark.parametrize(("schema", "key"), ACCEPTED.values(), ids=ACCEPTED.keys())
def test_accepted_params_schema(schema: Any, key: str) -> None:
    ver = _legacy(schema)
    assert _chat_param_key(ver) == key
    params = chat_entry_params(ChatEntry(allowed=True, reason=R.OK, param_key=key), "질문 내용")
    assert params == {key: "질문 내용"}
    _validate_against(params_schema_for_version(ver), params)  # stage에서 422가 나지 않는다


@pytest.mark.parametrize("schema", REJECTED.values(), ids=REJECTED.keys())
def test_rejected_params_schema(schema: Any) -> None:
    assert _chat_param_key(_legacy(schema)) is None


def test_standard_param_key_passes_stage_validation() -> None:
    ver = SimpleNamespace(runtime_profile=SKILL_RUNTIME_STANDARD, manifest={})
    assert _chat_param_key(ver) == "request"
    _validate_against(params_schema_for_version(ver), {"request": "요청"})


# ---------- legacy RAG 검색어 규칙 (#115) ----------

Q = _obj({"q": S}, ["q"])

RAG_CARRIES = {
    # 매니페스트 → (채팅 대상, 렌더한 검색어가 메시지를 담아야 하는가)
    "topic-default": ({"params_schema": _obj({"topic": S}, ["topic"])}, "topic", True),
    "explicit-key": ({"params_schema": Q, "rag_query": "{params.q} 관련 자료"}, "q", True),
    "whole-params": ({"params_schema": Q, "rag_query": "{params}"}, "q", True),
    "key-beside-other": (
        {"params_schema": Q, "rag_query": "{params.topic} {params.q}"},
        "q",
        True,
    ),
    "static-query": ({"params_schema": Q, "rag_query": "회의록"}, "q", False),
    # params를 참조하지 않는 런타임 변수는 고정 검색어로 본다(채팅 진입 run의 state에 있다)
    "chat-message-variable": ({"params_schema": Q, "rag_query": "{chat_message}"}, "q", False),
    # #114와 조합: minLength 1인 topic + 기본 검색어 — sl-agents 시드 복구 시나리오
    "topic-minLength-default-query": (
        {"params_schema": _obj({"topic": {**S, "minLength": 1}}, ["topic"])},
        "topic",
        True,
    ),
    # 검색 노드가 없으면 검색어를 보지 않는다(`build_nodes`와 같은 조건)
    "mode-none": ({"params_schema": Q, "input": {"mode": "none"}}, "q", False),
    "mode-full": ({"params_schema": Q, "input": {"mode": "full"}}, "q", False),
    "use-rag-false": ({"params_schema": Q, "use_rag": False}, "q", False),
    "no-note-access": ({"params_schema": Q, "permissions": {"rag": False}}, "q", False),
}

RAG_MISSES = {
    # 이슈 사례: 기본 검색어 `{params.topic}`인데 채팅 대상은 q다
    "default-query-other-key": {"params_schema": Q},
    "explicit-rag-mode": {"params_schema": Q, "input": {"mode": "rag"}},
    "optional-single-default-query": {"params_schema": _obj({"q": S})},  # 필수 없는 단일 후보
    "partial-template": {"params_schema": Q, "rag_query": "요약: {params.topic}"},
    "nested-path": {"params_schema": Q, "rag_query": "{params.q.text}"},
    "prefix-only-match": {"params_schema": _obj({"q": S}, ["q"]), "rag_query": "{params.qq}"},
    "non-string-query": {"params_schema": Q, "rag_query": 123},
    "bad-input": {"params_schema": Q, "input": "rag"},
}


@pytest.mark.parametrize(
    ("manifest", "key", "searches"), RAG_CARRIES.values(), ids=RAG_CARRIES.keys()
)
def test_rag_query_that_carries_the_message_is_accepted(
    manifest: Any, key: str, searches: bool
) -> None:
    from biznotes_core.services.agents.templating import render

    ver = SimpleNamespace(runtime_profile=SKILL_RUNTIME_LEGACY, manifest=manifest)
    assert _chat_param_key(ver) == key
    params = chat_entry_params(ChatEntry(allowed=True, reason=R.OK, param_key=key), "분기 실적")
    _validate_against(params_schema_for_version(ver), params)
    if searches:
        query = manifest.get("rag_query", "{params.topic}")
        assert "분기 실적" in render(query, {"params": params})


@pytest.mark.parametrize("manifest", RAG_MISSES.values(), ids=RAG_MISSES.keys())
def test_rag_query_that_drops_the_message_is_incompatible(manifest: Any) -> None:
    ver = SimpleNamespace(runtime_profile=SKILL_RUNTIME_LEGACY, manifest=manifest)
    assert _chat_param_key(ver) is None


async def test_pinned_run_skips_the_rag_query_rule(db: FakeDB) -> None:
    """박제된 run 재검사는 파라미터 규칙(#115 검색어 규칙 포함)을 건너뛴다 — 과거 run 불변."""
    world = World(db)
    world.published.manifest = {"params_schema": Q}  # 기본 검색어 {params.topic}, 대상 q
    entry = await assert_chat_entry_allowed(
        db, OTHER, world.wrapper, pinned_version_id=world.published.id
    )
    assert entry.allowed and entry.param_key is None


async def test_rag_query_mismatch_is_reported_as_incompatible(db: FakeDB) -> None:
    """목록·단건이 같은 순수 판정을 거쳐 `skill_params_incompatible`로 거부한다."""
    world = World(db)
    world.publish(world.freeze(params_schema=Q))  # 기본 검색어 {params.topic}
    for user in (OWNER, OTHER):
        entry = await resolve_chat_entry(db, user, world.wrapper)
        assert (entry.allowed, entry.reason) == (False, R.SKILL_PARAMS_INCOMPATIBLE)


@pytest.mark.parametrize("manifest", [None, [], {}, {"params_schema": None}])
def test_legacy_without_params_schema_is_incompatible(manifest: Any) -> None:
    ver = SimpleNamespace(runtime_profile=SKILL_RUNTIME_LEGACY, manifest=manifest)
    assert _chat_param_key(ver) is None


@pytest.mark.parametrize(
    "entry",
    [
        ChatEntry(allowed=False, reason=R.SKILL_PARAMS_INCOMPATIBLE),
        ChatEntry(allowed=False, reason=R.NOT_VISIBLE, param_key="topic"),
        ChatEntry(allowed=True, reason=R.OK),  # workflow 진입 — 기존 agent_params를 쓴다
    ],
)
def test_chat_entry_params_requires_an_allowed_skill_entry(entry: ChatEntry) -> None:
    with pytest.raises(ValueError):
        chat_entry_params(entry, "hi")


# ---------- 관문 (M1·A2·A5) ----------


async def test_gate_runs_the_execution_check_first(
    db: FakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    original_resolve = chat_entry._resolve

    async def execution(_db: Any, user: Any, agent: Any) -> None:
        order.append("execution")
        assert (user, agent) == (OTHER, world.wrapper)

    async def resolve(*args: Any, **kwargs: Any) -> ChatEntry:
        order.append("chat_entry")
        return await original_resolve(*args, **kwargs)

    monkeypatch.setattr(policy, "assert_agent_execution_allowed", execution)
    monkeypatch.setattr(chat_entry, "_resolve", resolve)
    world = World(db)
    assert (await assert_chat_entry_allowed(db, OTHER, world.wrapper)).allowed
    assert order == ["execution", "chat_entry"]

    async def denied(*_a: Any) -> None:
        order.append("denied")
        raise HTTPException(404, "Agent not found")

    order.clear()
    monkeypatch.setattr(policy, "assert_agent_execution_allowed", denied)
    with pytest.raises(HTTPException):
        await assert_chat_entry_allowed(db, OTHER, world.wrapper)
    assert order == ["denied"]  # 실행 인가가 막으면 채팅 판정까지 가지 않는다


async def test_gate_returns_the_entry_for_an_allowed_skill(db: FakeDB) -> None:
    world = World(db)
    entry = await assert_chat_entry_allowed(db, OTHER, world.wrapper)
    assert entry.allowed and entry.skill_version_id == world.published.id
    assert chat_entry_params(entry, "보고서") == {"topic": "보고서"}


async def test_gate_hides_why_a_runnable_skill_is_not_a_chat_entry(db: FakeDB) -> None:
    """미승인 org 스킬은 비소유자도 직접 실행할 수 있지만(실행 인가 통과) 채팅 진입은 404다."""
    world = World(db, visibility="org")
    await policy.assert_agent_execution_allowed(db, OTHER, world.wrapper)  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as exc:
        await assert_chat_entry_allowed(db, OTHER, world.wrapper)
    assert exc.value.status_code == 404
    detail = str(exc.value.detail)
    for secret in (
        world.skill.name,
        world.skill.slug,
        "pdf-helper",
        str(world.published.id),
        "org",
        "system",
        "visibility",
    ):
        assert secret not in detail


@pytest.mark.parametrize("hide", ["private", "disabled"])
async def test_gate_404_for_hidden_skills(db: FakeDB, hide: str) -> None:
    world = World(db, visibility="private") if hide == "private" else World(db, enabled=False)
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).reason == R.NOT_VISIBLE
    with pytest.raises(HTTPException) as exc:
        await assert_chat_entry_allowed(db, OTHER, world.wrapper)
    assert exc.value.status_code == 404
    assert world.skill.name not in str(exc.value.detail)


REJECTIONS = {
    # 채팅 판정이 막는다(실행 인가는 통과). org는 노출 version 승인이 없어서다(#410 D1)
    "org-unapproved": lambda w: setattr(w.skill, "visibility", "org"),
    "standard-flag-off": lambda w: w.publish(w.freeze(profile=SKILL_RUNTIME_STANDARD)),
    "params-incompatible": lambda w: setattr(w.published, "manifest", {"params_schema": NO_STRING}),
    # 실행 인가가 먼저 막는다
    "unpublished": lambda w: setattr(w.skill, "published_version_id", None),
    "private": lambda w: setattr(w.skill, "visibility", "private"),
    "skill-disabled": lambda w: setattr(w.skill, "enabled", False),
    "wrapper-disabled": lambda w: setattr(w.wrapper, "enabled", False),
}


@pytest.mark.parametrize("reject", REJECTIONS.values(), ids=REJECTIONS.keys())
async def test_every_rejection_is_the_same_404(db: FakeDB, reject: Any) -> None:
    """A2 — 어느 층이 어떤 사유로 막든 응답이 같다. 응답으로 사유나 존재를 가려낼 수 없다."""
    world = World(db)
    reject(world)
    with pytest.raises(HTTPException) as exc:
        await assert_chat_entry_allowed(db, OTHER, world.wrapper)
    assert (exc.value.status_code, exc.value.detail, exc.value.headers) == (
        404,
        "Agent not found",
        None,
    )


async def test_gate_keeps_the_workflow_rules(db: FakeDB) -> None:
    ok = workflow()
    assert (await assert_chat_entry_allowed(db, OTHER, ok)).reason == R.OK
    for agent, user in (
        (workflow(scope="user", owner_id=OWNER.id), ADMIN),  # admin 확대 없음
        (workflow(definition={"nodes": []}), OTHER),  # 채팅 형태가 아니다
        (workflow(enabled=False), OTHER),  # 실행 인가에서 막힌다
    ):
        with pytest.raises(HTTPException) as exc:
            await assert_chat_entry_allowed(db, user, agent)
        assert exc.value.status_code == 404
    draft = workflow(scope="user", owner_id=OWNER.id, enabled=False)
    assert (await assert_chat_entry_allowed(db, OWNER, draft)).allowed


# ---------- 박제 run 재검사 (N1·A5) ----------


async def test_pinned_run_survives_a_new_published_version(db: FakeDB) -> None:
    """박제 version이 옛 공개 version이고 새 공개 version은 파라미터 비호환이어도 허용한다."""
    world = World(db)
    old = world.published
    newer = world.freeze(params_schema=_obj({"a": S, "b": S}, ["a", "b"]))
    world.publish(newer)
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).reason == (
        R.SKILL_PARAMS_INCOMPATIBLE
    )
    entry = await assert_chat_entry_allowed(db, OTHER, world.wrapper, pinned_version_id=old.id)
    assert entry.allowed and entry.skill_version_id == old.id


async def test_owner_pinned_to_the_unpublished_latest_freeze_is_rechecked_ok(db: FakeDB) -> None:
    """system 스킬 소유자의 run은 미공개 최신 freeze를 박제한다 — 재검사도 그 version으로 한다."""
    world = World(db)
    latest = world.freeze(name="pdf-next")
    staged = await assert_chat_entry_allowed(db, OWNER, world.wrapper)
    assert staged.skill_version_id == latest.id
    world.freeze()  # 실행 전에 또 freeze해도 박제 run은 그대로다
    entry = await assert_chat_entry_allowed(db, OWNER, world.wrapper, pinned_version_id=latest.id)
    assert entry.skill_version_id == latest.id and entry.display_name == "pdf-next"


async def test_pinned_version_skips_the_params_rule(db: FakeDB) -> None:
    world = World(db)
    world.published.manifest = {"params_schema": _obj({"a": S, "b": S}, ["a", "b"])}
    entry = await assert_chat_entry_allowed(
        db, OTHER, world.wrapper, pinned_version_id=world.published.id
    )
    assert entry.allowed and entry.param_key is None


@pytest.mark.parametrize(
    "change", ["withdrawn", "skill-disabled", "wrapper-disabled", "private", "org-unapproved"]
)
async def test_pinned_run_is_rejected_when_exposure_is_revoked(db: FakeDB, change: str) -> None:
    """org 강등은 실행 인가를 통과한다(비소유자도 직접 실행 가능) — 박제 version이 승인되지
    않았으므로 채팅 판정이 막는다(#410 D1)."""
    world = World(db)
    pinned = world.published.id
    if change == "withdrawn":
        world.skill.published_version_id = None
    elif change == "skill-disabled":
        world.skill.enabled = False
    elif change == "wrapper-disabled":
        world.wrapper.enabled = False
    elif change == "org-unapproved":
        world.skill.visibility = "org"
    else:
        world.skill.visibility = "private"
    with pytest.raises(HTTPException) as exc:
        await assert_chat_entry_allowed(db, OTHER, world.wrapper, pinned_version_id=pinned)
    assert exc.value.status_code == 404


async def test_pinned_version_of_another_skill_is_rejected(db: FakeDB) -> None:
    world = World(db)
    stray = version(uuid.uuid4())
    db.add(stray)
    with pytest.raises(HTTPException):
        await assert_chat_entry_allowed(db, OTHER, world.wrapper, pinned_version_id=stray.id)
    missing = uuid.uuid4()
    with pytest.raises(HTTPException):
        await assert_chat_entry_allowed(db, OTHER, world.wrapper, pinned_version_id=missing)


async def test_pinned_standard_version_follows_the_runtime_flag(
    db: FakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """런타임 플래그는 박제 version으로 판정한다 — 현재 공개 version이 legacy여도 막힌다."""
    world = World(db)
    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true")
    reset_settings()
    try:
        standard = world.freeze(profile=SKILL_RUNTIME_STANDARD)
        world.publish(standard)
        await assert_chat_entry_allowed(db, OTHER, world.wrapper, pinned_version_id=standard.id)
        world.publish(world.freeze())  # 다시 legacy 공개 — 박제 run은 standard 그대로다
    finally:
        monkeypatch.delenv("SKILL_STANDARD_RUNTIME_ENABLED")
        reset_settings()
    with pytest.raises(HTTPException):
        await assert_chat_entry_allowed(db, OTHER, world.wrapper, pinned_version_id=standard.id)


async def test_pinned_argument_is_ignored_for_workflows(db: FakeDB) -> None:
    entry = await assert_chat_entry_allowed(db, OTHER, workflow(), pinned_version_id=uuid.uuid4())
    assert entry.reason == R.OK


# ---------- 본인·org 스킬과 슬래시 이름 (#410) ----------


@pytest.mark.parametrize("visibility", ["private", "org"])
async def test_owner_runs_the_latest_freeze_without_a_pointer(db: FakeDB, visibility: str) -> None:
    """소유자는 공개 포인터 없이 최신 freeze를 실행하고, 판정도 그 version이다."""
    world = World(db, visibility=visibility, published_version_id=None)
    latest = world.freeze(name="mine-next")
    entry = await resolve_chat_entry(db, OWNER, world.wrapper)
    assert (entry.allowed, entry.skill_version_id, entry.display_name) == (
        True,
        latest.id,
        "mine-next",
    )
    assert (entry.slash_name, entry.source) == ("pdf-helper-slug", "own")
    _, run_version = await resolve_wrapper_run_version(db, world.wrapper, OWNER.id)
    assert entry.skill_version_id == run_version.id
    world.skill.current_version_id = None  # freeze가 없다 — 실행할 version이 없다
    assert (await resolve_chat_entry(db, OWNER, world.wrapper)).reason == R.SKILL_NOT_PUBLISHED


async def test_org_non_owner_needs_the_exposed_version_approved(db: FakeDB) -> None:
    """D1 — 승인은 실행 규칙으로 푼 노출 version 기준이다. 다른 version의 승인은 쓰지 않는다."""
    world = World(db, visibility="org")
    latest = world.freeze(name="org-next")
    db.approved.add(latest.id)  # 미공개 freeze의 승인은 노출 version을 덮지 않는다
    for user in (OTHER, ADMIN):
        assert (await resolve_chat_entry(db, user, world.wrapper)).reason == (
            R.SKILL_NOT_PUBLISHED
        )
    db.approved.add(world.published.id)
    for user in (OTHER, ADMIN):
        entry = await resolve_chat_entry(db, user, world.wrapper)
        assert (entry.allowed, entry.skill_version_id, entry.display_name) == (
            True,
            world.published.id,
            "pdf-helper",
        )
        assert (entry.slash_name, entry.source) == ("pdf-helper-slug", "org")
        _, run_version = await resolve_wrapper_run_version(db, world.wrapper, user.id)
        assert entry.skill_version_id == run_version.id
    world.skill.published_version_id = None  # 포인터가 없으면 승인이 남아 있어도 거부
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).reason == R.SKILL_NOT_PUBLISHED
    owner = await resolve_chat_entry(db, OWNER, world.wrapper)
    assert (owner.allowed, owner.skill_version_id, owner.source) == (True, latest.id, "own")


async def test_system_scope_is_labelled_before_ownership(db: FakeDB) -> None:
    """system 스킬은 소유자에게도 `source="system"`이고 판정은 지금과 같다(포인터 필요)."""
    world = World(db)
    for user in (OWNER, OTHER):
        entry = await resolve_chat_entry(db, user, world.wrapper)
        assert (entry.allowed, entry.slash_name, entry.source) == (
            True,
            "pdf-helper-slug",
            "system",
        )
    world.skill.published_version_id = None
    for user in (OWNER, OTHER):
        entry = await resolve_chat_entry(db, user, world.wrapper)
        assert (entry.reason, entry.slash_name, entry.source) == (
            R.SKILL_NOT_PUBLISHED,
            None,
            None,
        )


@pytest.mark.parametrize(
    "manifest",
    [
        {"params_schema": TOPIC_SCHEMA, "permissions": {"http": True}},
        {"params_schema": TOPIC_SCHEMA, "permissions": {"mcp": True, "scripts": True}},
    ],
    ids=["http", "mcp-scripts"],
)
def test_own_and_org_skills_skip_the_run_skill_read_only_guard(
    db: FakeDB, manifest: dict[str, Any]
) -> None:
    """D2 — 사람이 고르는 진입이라 `run_skill`의 read-only 가드를 본인·org 스킬에도 쓰지 않는다."""
    from biznotes_core.services.agents.tool_registry import skill_manifest_is_read_only

    assert not skill_manifest_is_read_only(manifest)
    own = World(db, visibility="private", published_version_id=None)
    own.published.manifest = manifest
    assert decide(OWNER, own).allowed
    org = World(db, visibility="org")
    org.published.manifest = manifest
    assert decide(OTHER, org, org_approved=True).allowed


def test_workflow_slash_name_is_the_handle() -> None:
    """workflow는 `Agent.handle`이다. handle이 없으면 None이고 판정은 그대로 허용이다."""
    cases = [
        (workflow(handle="summarize"), ("summarize", "system")),
        (workflow(scope="user", owner_id=OWNER.id, handle="mine"), ("mine", "own")),
        (workflow(), (None, "system")),
    ]
    for agent, expected in cases:
        entry = _decide_workflow(OWNER, agent)
        assert entry.allowed and (entry.slash_name, entry.source) == expected
    for agent, user in (
        (workflow(definition={"nodes": []}, handle="broken"), OWNER),
        (workflow(scope="user", owner_id=OWNER.id, handle="mine"), OTHER),
    ):
        rejected = _decide_workflow(user, agent)
        assert not rejected.allowed and (rejected.slash_name, rejected.source) == (None, None)


def test_slash_metadata_does_not_change_entry_equality() -> None:
    """새 필드는 표시 메타데이터다 — 기존 동등 비교·해시에 들어가지 않는다."""
    bare = ChatEntry(allowed=True, reason=R.OK)
    labelled = ChatEntry(allowed=True, reason=R.OK, slash_name="x", source="own")
    assert labelled == bare and hash(labelled) == hash(bare)
    assert (bare.slash_name, bare.source) == (None, None)


async def test_pinned_org_run_needs_the_pinned_version_approved(db: FakeDB) -> None:
    """재검사는 박제 version의 승인을 본다 — 포인터가 옮겨 가도 승인된 박제는 통과한다."""
    world = World(db, visibility="org")
    approved = world.published
    db.approved.add(approved.id)
    staged = await assert_chat_entry_allowed(db, OTHER, world.wrapper)
    assert staged.skill_version_id == approved.id
    unapproved = world.freeze()
    world.publish(unapproved)  # 판정과 박제 사이에 포인터가 미승인 version으로 옮겨 갔다
    assert (await resolve_chat_entry(db, OTHER, world.wrapper)).reason == R.SKILL_NOT_PUBLISHED
    entry = await assert_chat_entry_allowed(
        db, OTHER, world.wrapper, pinned_version_id=approved.id
    )
    assert entry.allowed and entry.skill_version_id == approved.id
    with pytest.raises(HTTPException) as exc:  # 미승인 version을 박제한 run
        await assert_chat_entry_allowed(db, OTHER, world.wrapper, pinned_version_id=unapproved.id)
    assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")
    world.skill.published_version_id = None  # 노출 철회 — 승인된 박제도 거부
    with pytest.raises(HTTPException):
        await assert_chat_entry_allowed(db, OTHER, world.wrapper, pinned_version_id=approved.id)


@pytest.mark.parametrize("visibility", ["private", "org"])
async def test_owner_pinned_recheck_only_needs_enabled(db: FakeDB, visibility: str) -> None:
    """소유자 박제 재검사는 enabled만 본다 — 새 freeze·승인 없음·포인터 없음과 무관하다."""
    world = World(db, visibility=visibility, published_version_id=None)
    first = world.published
    staged = await assert_chat_entry_allowed(db, OWNER, world.wrapper)
    assert (staged.skill_version_id, staged.source) == (first.id, "own")
    world.freeze()
    entry = await assert_chat_entry_allowed(db, OWNER, world.wrapper, pinned_version_id=first.id)
    assert entry.allowed and entry.skill_version_id == first.id
    world.skill.enabled = False
    with pytest.raises(HTTPException) as exc:
        await assert_chat_entry_allowed(db, OWNER, world.wrapper, pinned_version_id=first.id)
    assert exc.value.status_code == 404


async def test_gate_404_for_another_users_private_wrapper(db: FakeDB) -> None:
    """다른 사용자의 private wrapper id는 승인 여부와 무관하게 같은 404다(존재 은닉)."""
    world = World(db, visibility="private", published_version_id=None)
    db.approved.add(world.published.id)
    assert (await assert_chat_entry_allowed(db, OWNER, world.wrapper)).allowed
    for user in (OTHER, ADMIN):
        with pytest.raises(HTTPException) as exc:
            await assert_chat_entry_allowed(db, user, world.wrapper)
        assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")


# ---------- 일괄 판정의 workflow 부분 ----------


async def test_batch_decides_workflows_without_queries(db: FakeDB) -> None:
    """workflow만 있으면 쿼리 없이 단건과 같은 판정이다(스킬 적재는 PG 테스트가 검증한다)."""
    agents = [
        workflow(),
        workflow(scope="user", owner_id=OWNER.id),
        workflow(enabled=False),
        workflow(definition={"nodes": []}),
    ]
    batch = await resolve_chat_entries(db, OTHER, agents)  # FakeDB에 execute가 없다
    assert batch == {a.id: await resolve_chat_entry(db, OTHER, a) for a in agents}


def test_reason_values_are_the_wire_contract() -> None:
    assert {r.value for r in R} == {
        "ok",
        "not_visible",
        "not_enabled",
        "workflow_shape",
        "skill_disabled",
        "skill_not_system",
        "skill_not_published",
        "skill_runtime_disabled",
        "skill_params_incompatible",
    }
    assert f"{R.OK}" == "ok"  # 제품이 문자열로 직렬화해도 값이 나온다
    assert chat_entry.__all__ == [
        "ChatEntry",
        "ChatEntryNameMatch",  # #410 추가
        "ChatEntryReason",
        "assert_chat_entry_allowed",
        "chat_entry_params",
        "list_chat_entry_agents",
        "resolve_chat_entries",
        "resolve_chat_entry",
        "resolve_chat_entry_by_name",  # #410 추가
        "resolve_wrapper_chat_entries",
    ]
