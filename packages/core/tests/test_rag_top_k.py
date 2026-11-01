"""발췌 수(`top_k`)는 생략하면 어드민 RAG 설정을 따른다 (#45).

경로마다 기본값 6을 박아 두면 어드민이 설정을 바꿔도 react 도구·워크플로우 노드·스킬
패키지의 발췌 수가 갈린다. 여기서는 해석 규칙 한 곳(`top_k_from_settings`)과, 생략을 6으로
채우지 않는 세 경로(노드 변환·스킬 빌더·매니페스트 검증)를 DB 없이 확인한다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from biznotes_core.services import embedding_spaces, rag_chat, settings_store
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.graph import kind_to_nodes
from biznotes_core.services.agents.nodes.retrieval import _exec_rag_search
from biznotes_core.services.embedding_spaces import LOCAL_SPACE
from biznotes_core.services.rag_chat import RetrievalResult, top_k_from_settings
from biznotes_core.services.skills.builder import LoadedSkill, build_nodes
from biznotes_core.services.skills.service import STARTER_MANIFEST
from biznotes_core.services.skills.validator import validate_manifest

# ── 해석 규칙 한 곳 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("setting", "requested", "expected"),
    [
        (10, None, 10),  # 생략 → 어드민 설정
        (10, 3, 3),  # 명시한 값이 설정보다 우선
        (10, "4", 4),  # JSON 인자가 문자열로 와도 정수로 해석
        (10, 0, 1),  # 범위(1~30) 밖은 잘라 낸다
        (10, -2, 1),
        (10, 500, 30),
        (999, None, 30),  # 설정 자체가 범위를 벗어나도 검색 limit으로 새지 않는다
    ],
)
def test_top_k_rule_is_one_place(setting: int, requested: Any, expected: int):
    assert top_k_from_settings({"top_k": setting}, requested) == expected


def test_top_k_falls_back_to_the_shipped_default_when_unset():
    """설정 행이 비어 있어도(키 없음) 종전 기본값 6으로 동작한다."""
    assert top_k_from_settings({}, None) == settings_store.DEFAULTS["rag"]["top_k"]


# ── retrieve_context: 생략 해석 + 리랭크 후보 하한 ──────────────────────


@pytest.fixture
def retrieval(monkeypatch):
    """retrieve_context가 부르는 설정·공간·검색을 가짜로 두고 검색 limit을 기록한다."""
    seen: dict[str, Any] = {"rag": {}}

    async def get_setting(db: Any, key: str) -> dict[str, Any]:
        assert key == "rag"
        return seen["rag"]

    async def fake_spaces(db: Any, notebook_ids: Any, source_ids: Any = None) -> list[Any]:
        return [LOCAL_SPACE]

    async def fake_search(
        db: Any, spaces: Any, query: str, *, limit: int = 8, **kw: Any
    ) -> list[Any]:
        seen["limit"] = limit
        return []

    monkeypatch.setattr(settings_store, "get_setting", get_setting)
    monkeypatch.setattr(embedding_spaces, "spaces_in_scope", fake_spaces)
    monkeypatch.setattr(embedding_spaces, "search_spaces", fake_search)
    return seen


class _NoRowsDB:
    async def execute(self, stmt: Any) -> Any:
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: []),
            tuples=lambda: SimpleNamespace(all=lambda: []),
        )


async def test_retrieve_context_without_top_k_follows_the_setting(retrieval):
    retrieval["rag"] = {"top_k": 11}

    await rag_chat.retrieve_context(_NoRowsDB(), uuid.uuid4(), "q", source_ids=None)  # type: ignore[arg-type]

    assert retrieval["limit"] == 11


async def test_rerank_candidates_never_shrink_the_excerpt_count(retrieval):
    """#45 참고 — `rerank_candidates`가 설정 top_k보다 작으면 발췌가 조용히 줄었다."""
    retrieval["rag"] = {"top_k": 20, "rerank_enabled": True, "rerank_candidates": 5}

    await rag_chat.retrieve_context(_NoRowsDB(), uuid.uuid4(), "q", source_ids=None)  # type: ignore[arg-type]

    assert retrieval["limit"] == 20


async def test_rerank_candidates_stay_the_candidate_pool_when_larger(retrieval):
    """후보가 top_k보다 크면 종전대로 후보 수만큼 가져와 재정렬한다."""
    retrieval["rag"] = {"top_k": 6, "rerank_enabled": True, "rerank_candidates": 20}

    await rag_chat.retrieve_context(_NoRowsDB(), uuid.uuid4(), "q", source_ids=None)  # type: ignore[arg-type]

    assert retrieval["limit"] == 20


# ── 생략을 6으로 채우지 않는 경로 ───────────────────────────────────────


def _search_node(nodes: list[dict]) -> dict:
    return next(n for n in nodes if n["type"] == "rag_search")


@pytest.mark.parametrize(
    ("definition", "expected"),
    [({"prompt_template": "p"}, None), ({"prompt_template": "p", "top_k": 12}, 12)],
)
def test_skill_definition_keeps_omission_as_omission(definition: dict, expected: int | None):
    nodes, _ = kind_to_nodes("skill", definition)

    assert _search_node(nodes)["top_k"] == expected


def _loaded(manifest: dict[str, Any]) -> LoadedSkill:
    return LoadedSkill(
        metadata_name="s",
        metadata_description="d",
        instructions="본문",
        manifest={"mode": "instruction", "input": {"mode": "rag"}, **manifest},
    )


@pytest.mark.parametrize(("manifest", "expected"), [({}, None), ({"top_k": 9}, 9)])
def test_skill_package_keeps_omission_as_omission(manifest: dict, expected: int | None):
    nodes, _ = build_nodes(_loaded(manifest))

    assert _search_node(nodes)["top_k"] == expected


@pytest.mark.parametrize(("manifest", "expected"), [({}, None), ({"top_k": "7"}, 7)])
def test_validate_manifest_does_not_fill_top_k(manifest: dict, expected: int | None):
    assert validate_manifest(manifest)["top_k"] == expected


@pytest.mark.parametrize(("node", "expected"), [({}, None), ({"top_k": 8}, 8)])
async def test_workflow_node_hands_the_omission_to_retrieve_context(monkeypatch, node, expected):
    """노드는 미리 해석하지 않는다 — 검색을 건너뛰는 경우 설정을 읽지 않기 위해서다."""
    seen: dict[str, Any] = {}

    async def fake(db: Any, notebook_id: Any, query: str, source_ids: Any, top_k: Any = None):
        seen["top_k"] = top_k
        return RetrievalResult(citations=[], insufficient_context=False)

    monkeypatch.setattr(rag_chat, "retrieve_context", fake)
    ctx = RunContext(run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=None, params={})  # type: ignore[arg-type]

    await _exec_rag_search(ctx, {"query": "q", **node}, {})

    assert seen["top_k"] == expected


def test_starter_manifest_leaves_top_k_to_the_setting():
    """새로 만든 스킬이 6을 고정하지 않는다 — 작성자가 필요할 때만 명시한다."""
    assert "top_k" not in STARTER_MANIFEST
