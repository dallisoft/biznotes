"""스튜디오 카드 바인딩 계약(#180 — 선례 `test_mindmap_contract.py::test_web_binding_literals`).

웹 레지스트리(`apps/web/src/lib/studio-registry.ts`)의 카드 리터럴 — 스킬 slug,
에이전트 이름·kind, 대표 산출물 이름 — 이 시드 정의(`scripts/seed.py`)에 있고
서로 맞는지 대조한다.
- 스킬 바인딩: `SEED_SKILL_PACKAGES`(또는 마인드맵 시스템 스킬)의 slug,
  산출물 = 매니페스트 `artifact.name`.
- 에이전트 바인딩: `SEED_AGENTS`의 이름 ∧ kind, 산출물 = 워크플로우 노드가 저장하는
  이름 또는 스킬 `artifact_name`. 같은 이름의 스킬 패키지가 있으면
  `upsert_skill_packages`가 레거시 에이전트를 끄므로 바인딩이 비활성이 된다.
"""

import re
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
STUDIO_REGISTRY = API_DIR.parent / "web/src/lib/studio-registry.ts"

_SKILL = re.compile(r'binding: \{ type: "skill", slug: ("[^"]+"|\w+) \}')
_AGENT = re.compile(r'binding: \{ type: "agent", kind: "(\w+)", name: "([^"]+)" \}')
_ARTIFACT = re.compile(r'artifactName: ("[^"]*"|\w+),')
_ID = re.compile(r'id: "([^"]+)",')


def _cards() -> list[dict]:
    """`STUDIO_CARDS` 배열의 카드 객체 리터럴을 하나씩 읽는다(상수 참조는 이름 그대로 둔다)."""
    text = STUDIO_REGISTRY.read_text(encoding="utf-8")
    body = text.split("export const STUDIO_CARDS: StudioCard[] = [", 1)[1].split("\n];", 1)[0]
    cards = []
    for block in body.split("\n  },"):
        if "id:" not in block:
            continue
        skill, agent = _SKILL.search(block), _AGENT.search(block)
        card_id, artifact = _ID.search(block), _ARTIFACT.search(block)
        assert card_id and artifact, block
        cards.append(
            {
                "id": card_id.group(1),
                "artifact": artifact.group(1).strip('"'),
                "skill": skill.group(1).strip('"') if skill else None,
                "agent": (agent.group(1), agent.group(2)) if agent else None,
            }
        )
    return cards


def _agent_artifacts(spec: dict) -> set[str]:
    definition = spec["definition"]
    if spec["kind"] == "skill":
        return {definition["artifact_name"]}
    names = {
        n["name"]
        for n in definition["nodes"]
        if n["type"] == "save_artifact" and "name" in n
    }
    # image_generate 이름은 템플릿이다 — 값 하나(1)를 넣고 코어가 붙이는 PNG 확장자를 단다
    names |= {
        re.sub(r"\{[^}]+\}", "1", n["name"]) + ".png"
        for n in definition["nodes"]
        if n["type"] == "image_generate" and "name" in n
    }
    return names


def test_card_literals_parsed():
    cards = _cards()
    assert [c["id"] for c in cards] == [
        "mindmap",
        "report",
        "briefing",
        "study-guide",
        "quiz",
        "flashcards",
        "slide-deck",
        "slide-deck-outline",
        "slide-deck-image",
        "slide-deck-write",
    ]
    for c in cards:
        assert (c["skill"] is None) != (c["agent"] is None), c


def test_skill_bindings_match_seed_packages():
    from scripts.seed import SEED_SKILL_PACKAGES

    packages = {p["slug"]: p for p in SEED_SKILL_PACKAGES}
    for c in _cards():
        if c["skill"] is None or c["skill"] == "MINDMAP_SKILL_SLUG":
            continue  # 마인드맵은 test_mindmap_contract.py::test_web_binding_literals가 대조한다
        assert c["skill"] in packages, f"{c['id']}: 시드 스킬 패키지에 {c['skill']} 없음"
        assert packages[c["skill"]]["artifact"]["name"] == c["artifact"], c["id"]


def test_agent_bindings_match_seed_agents():
    from scripts.seed import SEED_AGENTS, SEED_SKILL_PACKAGES

    packaged_names = {p["name"] for p in SEED_SKILL_PACKAGES}
    for c in _cards():
        if c["agent"] is None:
            continue
        kind, name = c["agent"]
        specs = [s for s in SEED_AGENTS if s["name"] == name]
        assert len(specs) == 1, f"{c['id']}: 시드 에이전트 {name!r}가 하나가 아니다"
        assert specs[0]["kind"] == kind, c["id"]
        if c["artifact"] == "":
            # 산출물을 저장하지 않는 loop 본문(「슬라이드 장 작성」, #218) — 결과를 loop로 돌려준다
            assert not _agent_artifacts(specs[0]), c["id"]
            assert "output" in specs[0]["definition"], c["id"]
        else:
            assert c["artifact"] in _agent_artifacts(specs[0]), c["id"]
        # 같은 이름의 스킬 패키지가 생기면 레거시 에이전트가 꺼진다 — 그때는 스킬 바인딩으로 옮긴다
        assert name not in packaged_names, f"{c['id']}: {name!r}는 스킬 패키지로 대체됨"


def test_mindmap_binding_constants():
    text = STUDIO_REGISTRY.read_text(encoding="utf-8")
    assert re.search(r"artifactName: MINDMAP_ARTIFACT_NAME,", text)
    assert re.search(r"binding: \{ type: \"skill\", slug: MINDMAP_SKILL_SLUG \}", text)
