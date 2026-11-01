"""슬라이드 자료 삽화(core-v0.23.0-upgrade PR-2, AC-8·AC-9·AC-11·AC-12).

- 시드: 덱 워크플로우가 저장 뒤 삽화 묘사 llm(`illustrate`) → 검증(`illustrations`) → `loop`
  (`items: {illustrations.json.images}`, `max_items: {params.image_count}`)로 본문 워크플로우
  「슬라이드 삽화」(`image_generate`)를 부른다(D1-A). 장수 0이면 `wants_images` 조건이 묘사
  llm부터 건너뛴다.
- 프롬프트 출처: 이미지 프롬프트는 묘사 llm 출력(검증한 `illustrations`)에서만 온다. 코어
  `prompt_provenance`를 그대로 불러 판정한다 — 소스 요약(`digest.*`)이나 `json_compose`로 조립한 덱
  (`validate`·`compose`)을 쓰면 실패한다(AC-12, #218 — 코어 README v0.25.0).
- 런타임: 코어 그래프를 실제로 돌린다. 외부 경계(LLM 게이트웨이·검색·객체 저장)만 대역이고, 이미지는
  코어 mock 프로바이더가 결정적 PNG를 낸다. 덱 본문은 #218 흐름(개요 → 장별 작성 loop → 조립)으로
  만든다(`tests/slide_deck_fakes.py`).
- `GET /api/v1/models?modality=image`(D4): 비인증 401, 잘못된 값 422, 이미지 모델만 돌려준다.
"""

import asyncio
import contextlib
import copy
import json
import uuid
from pathlib import Path

import httpx
import pytest
from biznotes_core.models import Agent, LLMModel, LLMProvider
from biznotes_core.services.agent_runs import ParamsInvalid, validate_params
from biznotes_core.services.agents.prompt_provenance import (
    check_image_prompt_sources,
    image_safe_roots,
    loop_items_image_safe,
)
from biznotes_core.services.agents.runtime import AgentRuntimeError, RunContext, execute_agent
from biznotes_core.services.agents.templating import _VAR_RE
from structlog.testing import capture_logs

from app.services.image_prompts import find_unsafe_image_prompts
from scripts.seed import (
    _SLIDE_ILLUSTRATE_MAX_TOKENS,
    _SLIDE_ILLUSTRATE_PROMPT,
    SEED_AGENTS,
    SLIDE_DECK_AGENT_NAME,
    SLIDE_IMAGE_AGENT_NAME,
    SLIDE_IMAGE_MAX,
    SLIDE_OUTLINE_AGENT_NAME,
    SLIDE_WRITE_AGENT_NAME,
    link_loop_bodies,
)
from tests.conftest import bearer, login
from tests.slide_deck_fakes import SlideLLM

WEB_SRC = Path(__file__).resolve().parents[2] / "web/src"


def _spec(name: str) -> dict:
    [spec] = [s for s in SEED_AGENTS if s["name"] == name]
    return spec


def _nodes(definition: dict) -> dict[str, dict]:
    return {n["id"]: n for n in definition["nodes"]}


# ---------- 시드 계약 ----------


def test_deck_runs_image_loop_after_save():
    definition = _spec(SLIDE_DECK_AGENT_NAME)["definition"]
    nodes = _nodes(definition)
    # 덱을 먼저 저장한다 — 이미지 단계가 실패해도 덱은 남는다
    assert definition["edges"][-4:] == [
        ["save", "wants_images"],
        ["wants_images", "illustrate"],
        ["illustrate", "illustrations"],
        ["illustrations", "images"],
    ]
    # 장수 0이면 묘사 llm부터 건너뛴다(false_node 없음 = 끝)
    assert nodes["wants_images"] == {
        "id": "wants_images",
        "type": "condition",
        "left": "{params.image_count}",
        "op": ">",
        "right": "0",
        "true_node": "illustrate",
    }
    llm = {
        "system": nodes["illustrate"]["system"],
        "prompt": _SLIDE_ILLUSTRATE_PROMPT,
        "max_tokens": _SLIDE_ILLUSTRATE_MAX_TOKENS,
        "continue_on_error": True,
    }
    assert nodes["illustrate"] == {"id": "illustrate", "type": "llm", **llm}
    # 재시도는 validate 노드 사본으로 llm을 다시 부른다 — 원 노드와 같은 프롬프트·상한(#200)
    assert nodes["illustrations"] == {
        "id": "illustrations",
        "type": "json_validate",
        "schema": "slide_deck",
        "source": "{illustrate.text}",
        **llm,
    }
    assert nodes["images"] == {
        "id": "images",
        "type": "loop",
        "items": "{illustrations.json.images}",
        "max_items": "{params.image_count}",
        "item_var": "item",
        "parallel": True,
        "loop_body_agent_id": "",
        "continue_on_error": True,
    }


def test_image_body_workflow():
    spec = _spec(SLIDE_IMAGE_AGENT_NAME)
    assert spec["kind"] == "workflow"
    [art] = spec["definition"]["nodes"]
    assert spec["definition"]["edges"] == []
    assert art["type"] == "image_generate"
    # 산출물 이름의 n = 슬라이드 번호(웹 parse.ts SLIDE_IMAGE_NAME과 같은 규칙)
    assert art["name"] == "slide-image-{params.item.slide}"
    assert art["aspect_ratio"] == "2:3"
    assert "n" not in art  # 노드 한 번에 1장 — 장수는 loop max_items가 정한다
    assert art["continue_on_error"] is True  # 항목 하나의 실패가 형제를 취소하지 않는다
    assert {m.group(1) for m in _VAR_RE.finditer(art["prompt"])} == {"params.item.prompt"}
    # 웹 뷰어는 같은 이름 규칙으로 장에 붙이고, 숨김 카드로 탭에서 감춘다
    parser = (WEB_SRC / "lib/slide-deck/parse.ts").read_text(encoding="utf-8")
    name_re = r"/^slide-image-([1-9][0-9]{0,2})\.(?:png|jpe?g)$/"
    assert f"export const SLIDE_IMAGE_NAME = {name_re};" in parser
    registry = (WEB_SRC / "lib/studio-registry.ts").read_text(encoding="utf-8")
    binding = f'binding: {{ type: "agent", kind: "workflow", name: "{SLIDE_IMAGE_AGENT_NAME}" }}'
    assert binding in registry
    flow = (WEB_SRC / "lib/slide-deck/flow.ts").read_text(encoding="utf-8")
    assert f"export const SLIDE_IMAGE_MAX = {SLIDE_IMAGE_MAX};" in flow


@pytest.mark.parametrize("spec", SEED_AGENTS, ids=lambda s: s["name"])
def test_every_seed_agent_fits_the_agent_response_model(spec):
    """에이전트 목록 응답(`AgentOut.params_schema` = 코어 `ParamsSchema`)이 받는 형태다.

    속성 타입은 원시 타입만이다. 하나라도 어긋나면 `GET /agents?kind=…` 전체가 500이다
    (PR-2 라이브 확인에서 찾음).
    """
    from biznotes_core.schemas.agent import ParamsSchema

    ParamsSchema.model_validate(spec["params_schema"])


async def test_agents_list_serves_seeded_slide_workflows(client: httpx.AsyncClient, db, user_a):
    names = [
        SLIDE_DECK_AGENT_NAME,
        SLIDE_OUTLINE_AGENT_NAME,
        SLIDE_IMAGE_AGENT_NAME,
        SLIDE_WRITE_AGENT_NAME,
    ]
    suffix = uuid.uuid4().hex[:6]
    for name in names:
        agent = Agent(scope="system", enabled=True, **_spec(name))
        agent.name = f"{name}-{suffix}"
        db.add(agent)
    await db.commit()
    token = await login(client, user_a.email)
    res = await client.get("/api/v1/agents", params={"kind": "workflow"}, headers=bearer(token))
    assert res.status_code == 200, res.text
    listed = {a["name"] for a in res.json()}
    assert {f"{n}-{suffix}" for n in names} <= listed


def test_link_loop_bodies_fills_both_deck_loops():
    """덱의 loop 두 개(장 작성·삽화)에 각자의 본문 에이전트 id를 채운다."""
    image_id, write_id = str(uuid.uuid4()), str(uuid.uuid4())
    deck = _spec(SLIDE_DECK_AGENT_NAME)["definition"]
    ids = {SLIDE_IMAGE_AGENT_NAME: image_id, SLIDE_WRITE_AGENT_NAME: write_id}
    linked = link_loop_bodies(SLIDE_DECK_AGENT_NAME, deck, ids)
    assert _nodes(linked)["images"]["loop_body_agent_id"] == image_id
    assert _nodes(linked)["slides"]["loop_body_agent_id"] == write_id
    # 원본(시드 상수)은 그대로
    assert {_nodes(deck)[n]["loop_body_agent_id"] for n in ("images", "slides")} == {""}
    loops = [n["id"] for n in linked["nodes"] if n["type"] == "loop"]
    assert sorted(loops) == ["images", "slides"]
    outline = _spec(SLIDE_OUTLINE_AGENT_NAME)["definition"]
    assert link_loop_bodies(SLIDE_OUTLINE_AGENT_NAME, outline, {}) is outline


def test_illustrate_prompt_asks_images_within_count():
    assert "{params.image_count}" in _SLIDE_ILLUSTRATE_PROMPT
    assert '"images"' in _SLIDE_ILLUSTRATE_PROMPT
    assert '"images": [{"slide": int, "prompt": str}]' in _SLIDE_ILLUSTRATE_PROMPT
    # 저장한 덱(제목·슬라이드)을 읽는다 — 앞선 LLM 출력이라 작성자 블록 안이다
    block = '<untrusted_data kind="tool">\n{validate.json.title}\n{validate.json.slides}\n'
    assert block in _SLIDE_ILLUSTRATE_PROMPT


def test_image_count_param_bounds():
    deck = Agent(scope="system", enabled=True, **_spec(SLIDE_DECK_AGENT_NAME))
    outline = Agent(scope="system", enabled=True, **_spec(SLIDE_OUTLINE_AGENT_NAME))
    base = {"format": "detailed", "length": "default", "language": "한국어"}
    for count in (0, SLIDE_IMAGE_MAX):
        validate_params(deck, {**base, "image_count": count})
        validate_params(outline, {**base, "image_count": count})
    validate_params(outline, base)  # 개요는 장수를 쓰지 않는다 — 「계속」에 넘기려고 받아 둘 뿐
    for params in (
        base,  # 빈 max_items는 loop 실패 — 덱은 필수
        {**base, "image_count": SLIDE_IMAGE_MAX + 1},
        {**base, "image_count": -1},
        {**base, "image_count": "4"},
    ):
        with pytest.raises(ParamsInvalid):
            validate_params(deck, params)


# ---------- 프롬프트 출처(AC-12) ----------


def _image_prompt_problems(deck: dict, body: dict) -> list[str]:
    """덱의 loop 항목 출처 → 본문 params 안전 여부 → 본문 이미지 프롬프트 판정(코어 규칙 그대로)."""
    problems = []
    deck_roots = image_safe_roots(_nodes(deck), params_safe=True)
    loop = _nodes(deck)["images"]
    items = loop["items"]
    path = items[1:-1].strip() if items.startswith("{") and items.endswith("}") else None
    params_safe = loop_items_image_safe(items, deck_roots, resolved_path=path)
    body_nodes = _nodes(body)
    body_roots = image_safe_roots(body_nodes, params_safe=params_safe)
    for node in body_nodes.values():
        if node["type"] == "image_generate":
            try:
                check_image_prompt_sources(node, body_roots)
            except AgentRuntimeError as e:
                problems.append(str(e))
    return problems


def test_seed_image_prompts_come_only_from_illustrate_llm():
    deck = _spec(SLIDE_DECK_AGENT_NAME)["definition"]
    body = _spec(SLIDE_IMAGE_AGENT_NAME)["definition"]
    assert _image_prompt_problems(deck, body) == []
    # 저장 시점 검사(D2)도 두 정의를 통과시킨다
    assert find_unsafe_image_prompts(deck["nodes"]) == []
    assert find_unsafe_image_prompts(body["nodes"]) == []


@pytest.mark.parametrize(
    "items",
    [
        "{digest.context}",
        "{digest.sources}",
        # json_compose로 조립한 덱은 이미지 프롬프트 출처로 안전하지 않다(코어 README v0.25.0) —
        # 그래서 묘사 llm을 따로 둔다(#218)
        "{compose.json.images}",
        "{validate.json.images}",
    ],
)
def test_loop_items_from_unsafe_sources_fail_provenance(items):
    """묘사 llm을 거치지 않은 항목(소스 요약·조립한 덱)을 돌리면 본문 이미지 프롬프트가 거부된다."""
    deck = copy.deepcopy(_spec(SLIDE_DECK_AGENT_NAME)["definition"])
    _nodes(deck)["images"]["items"] = items
    body = _spec(SLIDE_IMAGE_AGENT_NAME)["definition"]
    [problem] = _image_prompt_problems(deck, body)
    assert "image_generate art" in problem


@pytest.mark.parametrize("source", ["{digest.context}", "{validate.text}", "{compose.text}"])
def test_illustrations_fed_by_unsafe_text_fails_provenance(source):
    deck = copy.deepcopy(_spec(SLIDE_DECK_AGENT_NAME)["definition"])
    _nodes(deck)["illustrations"]["source"] = source
    body = _spec(SLIDE_IMAGE_AGENT_NAME)["definition"]
    assert len(_image_prompt_problems(deck, body)) == 1


# ---------- 런타임(AC-8·AC-9) ----------


@pytest.fixture
def llm(monkeypatch, _mock_runtime_deps) -> SlideLLM:
    """LLM 게이트웨이 경계 대역 — 개요(6장)·장 작성·삽화 묘사 응답. 검색은 결과 없음(conftest)."""
    fake = SlideLLM()
    monkeypatch.setattr(
        "biznotes_core.services.gateway.llm.complete_stream_with_fallback", fake.stream
    )
    return fake


@pytest.fixture
async def image_model(db) -> LLMModel:
    """코어 mock 이미지 모델(결정적 8×8 PNG). 기본 모델 해석이 이 행을 고르게 기본으로 둔다."""
    provider = LLMProvider(name=f"mock-img-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-image",
        display_name="Mock Image",
        capabilities=["image"],
        cost_per_1k={"per_image": 0.04},
        is_default=True,
        enabled=True,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


@pytest.fixture
def image_calls(monkeypatch):
    """이미지 게이트웨이 호출 기록 — 코어 mock 경로를 그대로 부르고 프롬프트·크기만 남긴다."""
    from biznotes_core.services.gateway import image as gateway_image

    calls: list[dict] = []
    real = gateway_image.generate_images

    async def recording(resolved, prompt, *, n, size):
        calls.append({"prompt": prompt, "n": n, "size": size})
        return await real(resolved, prompt, n=n, size=size)

    monkeypatch.setattr(gateway_image, "generate_images", recording)
    return calls


async def _linked_deck(db) -> dict:
    """시드처럼 loop 본문 두 개를 시스템 에이전트로 만들고 덱 loop에 id를 채운다."""
    ids = {}
    for name in (SLIDE_IMAGE_AGENT_NAME, SLIDE_WRITE_AGENT_NAME):
        body = Agent(scope="system", enabled=True, **_spec(name))
        body.name = f"{name}-{uuid.uuid4().hex[:6]}"
        db.add(body)
        await db.flush()
        ids[name] = str(body.id)
    await db.commit()
    deck = _spec(SLIDE_DECK_AGENT_NAME)["definition"]
    return link_loop_bodies(SLIDE_DECK_AGENT_NAME, deck, ids)


def _ctx(db, image_count: float) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=uuid.uuid4(),
        db=db,
        params={
            "format": "detailed",
            "length": "short",
            "language": "한국어",
            "description": "",
            "image_count": image_count,
        },
    )


SIX_IMAGES = [{"slide": n, "prompt": f"원두 그림 {n}"} for n in (2, 3, 5, 6, 1, 4)]


async def test_images_off_saves_deck_without_calling_image_node(
    db, llm, _mock_runtime_deps, image_model, image_calls
):
    """AC-8: 장수 0이면 묘사 llm도 본문도 부르지 않는다 — 덱 결과는 지금과 같다."""
    llm.images = SIX_IMAGES
    ctx = _ctx(db, 0)
    await execute_agent(ctx, "workflow", await _linked_deck(db))

    assert llm.calls_of("illustrate") == []
    assert image_calls == []
    assert [a["name"] for a in ctx.artifact_keys] == ["slide-deck.json"]
    assert ctx.usage.get("images", 0) == 0


async def test_images_on_makes_at_most_four_named_by_slide(
    db, llm, _mock_runtime_deps, image_model, image_calls
):
    """AC-9: 켜면 앞에서부터 최대 4장, 이름은 slide-image-<슬라이드 번호>.png다."""
    llm.images = SIX_IMAGES
    ctx = _ctx(db, SLIDE_IMAGE_MAX)
    await execute_agent(ctx, "workflow", await _linked_deck(db))

    # 개요 1 + 장 작성 6 + 삽화 묘사 1 — 묘사는 저장한 덱을 읽는다
    assert [len(llm.calls_of(k)) for k in ("outline", "write", "illustrate")] == [1, 6, 1]
    [illustrate] = llm.calls_of("illustrate")
    assert illustrate["max_tokens"] == _SLIDE_ILLUSTRATE_MAX_TOKENS
    assert all(f'"heading": "장 {n}"' in illustrate["prompt"] for n in range(1, 7))
    assert len(image_calls) == SLIDE_IMAGE_MAX
    assert {c["size"] for c in image_calls} == {"1024x1536"}  # 2:3
    assert {c["n"] for c in image_calls} == {1}
    assert sorted(c["prompt"].rsplit("장면: ", 1)[1] for c in image_calls) == [
        "원두 그림 2",
        "원두 그림 3",
        "원두 그림 5",
        "원두 그림 6",
    ]
    names = [a["name"] for a in ctx.artifact_keys]
    assert names[0] == "slide-deck.json"  # 덱이 첫 산출물(run 상세가 먼저 연다)
    assert sorted(names[1:]) == [
        "slide-image-2.png",
        "slide-image-3.png",
        "slide-image-5.png",
        "slide-image-6.png",
    ]
    for ref in ctx.artifact_keys[1:]:
        assert ref["content_type"] == "image/png"
        assert _mock_runtime_deps[ref["key"]].startswith(b"\x89PNG\r\n\x1a\n")
    assert ctx.usage["images"] == SLIDE_IMAGE_MAX
    # 덱 JSON에는 묘사가 없다 — 웹은 산출물 이름으로 장에 붙인다(parse.ts SLIDE_IMAGE_NAME)
    saved = json.loads(_mock_runtime_deps[ctx.artifact_keys[0]["key"]])
    assert "images" not in saved
    assert len(saved["slides"]) == 6


async def test_illustrations_number_the_saved_slides(
    db, llm, _mock_runtime_deps, image_model, image_calls
):
    """실패해 빠진 장이 있으면 묘사 llm은 저장된 덱의 번호를 본다 — 삽화가 다른 장에 붙지 않는다."""
    llm.broken_titles = {"장 2"}
    llm.images = [{"slide": 5, "prompt": "마지막 장"}, {"slide": 6, "prompt": "범위 밖"}]
    ctx = _ctx(db, SLIDE_IMAGE_MAX)
    await execute_agent(ctx, "workflow", await _linked_deck(db))

    [illustrate] = llm.calls_of("illustrate")
    assert '"heading": "장 2"' not in illustrate["prompt"]
    assert [a["name"] for a in ctx.artifact_keys] == ["slide-deck.json", "slide-image-5.png"]


async def test_deck_without_images_field_runs_no_images(
    db, llm, _mock_runtime_deps, image_model, image_calls
):
    llm.images = None
    ctx = _ctx(db, SLIDE_IMAGE_MAX)
    await execute_agent(ctx, "workflow", await _linked_deck(db))
    assert image_calls == []
    assert [a["name"] for a in ctx.artifact_keys] == ["slide-deck.json"]


async def test_invalid_items_are_dropped_before_the_image_loop(
    db, llm, _mock_runtime_deps, image_model, image_calls
):
    """AC-15(#345): 잘못된 항목은 `illustrations`(코어 v0.23.2 `slide_deck`)에서 빠진다 — 범위 밖·
    비정수·중복 slide, 빈·상한을 넘는 prompt, 객체가 아닌 항목. 빠진 항목은 `max_items` 자리를
    차지하지 않는다."""
    llm.images = [
        {"slide": 9, "prompt": "범위 밖"},
        {"slide": "3", "prompt": "문자열 번호"},
        {"slide": 2.0, "prompt": "실수 번호"},
        {"slide": True, "prompt": "불리언 번호"},
        {"slide": "../x", "prompt": "경로"},
        {"slide": 2, "prompt": "  "},
        {"slide": 4, "prompt": "가" * 5000},
        "원두",
        {"slide": 2, "prompt": "원두"},
        {"slide": 2, "prompt": "중복"},
        {"slide": 3, "prompt": "추출"},
        {"slide": 4, "prompt": "로스팅"},
        {"slide": 5, "prompt": "포장"},
        {"slide": 6, "prompt": "시음"},
    ]
    ctx = _ctx(db, SLIDE_IMAGE_MAX)
    with capture_logs() as logs:
        await execute_agent(ctx, "workflow", await _linked_deck(db))

    names = [a["name"] for a in ctx.artifact_keys]
    assert names[0] == "slide-deck.json"
    # 유효한 5개 중 앞의 4개 — 잘못된 9개가 앞에 있어도 4장이 다 찬다
    assert sorted(names[1:]) == [f"slide-image-{n}.png" for n in (2, 3, 4, 5)]
    assert sorted(c["prompt"].rsplit("장면: ", 1)[1] for c in image_calls) == [
        "로스팅",
        "원두",
        "추출",
        "포장",
    ]
    [dropped] = [e for e in logs if e["event"] == "json_validate slide_deck images dropped"]
    assert dropped["dropped"] == 9


async def test_non_array_images_run_no_images(
    db, llm, _mock_runtime_deps, image_model, image_calls
):
    """AC-15(#345): `images`가 배열이 아니면 빈 배열이 된다 — loop가 객체를 항목 하나로 감싸지
    않는다."""
    llm.images = {"slide": 2, "prompt": "원두"}
    ctx = _ctx(db, SLIDE_IMAGE_MAX)
    await execute_agent(ctx, "workflow", await _linked_deck(db))

    assert image_calls == []
    assert [a["name"] for a in ctx.artifact_keys] == ["slide-deck.json"]


async def test_integral_float_image_count_makes_four(
    db, llm, _mock_runtime_deps, image_model, image_calls
):
    """AC-16(#346): `image_count: 4.0`(JSON Schema integer를 통과하는 수)도 4장이다."""
    deck = Agent(scope="system", enabled=True, **_spec(SLIDE_DECK_AGENT_NAME))
    validate_params(deck, {**_ctx(db, 4).params, "image_count": 4.0})  # 실행 생성 검증은 통과한다
    llm.images = SIX_IMAGES
    ctx = _ctx(db, 4.0)
    await execute_agent(ctx, "workflow", await _linked_deck(db))

    assert len(image_calls) == 4
    assert len(ctx.artifact_keys) == 5


async def test_provider_error_on_one_slide_keeps_its_siblings(
    db, llm, _mock_runtime_deps, image_model, monkeypatch
):
    """D7: 한 장의 프로바이더 오류(429·안전 필터 거부 등)는 그 장만 뺀다 — 본문 노드
    `continue_on_error`가 없으면 병렬 형제 장까지 취소된다."""
    from biznotes_core.services.gateway import image as gateway_image
    from biznotes_core.services.gateway.llm import GatewayCallError

    real = gateway_image.generate_images
    # 형제 장은 실패가 난 뒤에야 끝나게 묶는다 — mock 이미지는 즉시 끝나서, 묶지 않으면 형제가
    # 실패 전파 전에 완료돼 취소 회귀(본문 `continue_on_error` 제거)를 놓친다(PR-3 리뷰 M2).
    # 분기 세션이 없어 항목이 한 번에 하나씩 돌 때도 멈추지 않게 대기에 상한을 둔다
    released = asyncio.Event()

    async def flaky(resolved, prompt, *, n, size):
        if "원두 그림 3" in prompt:
            asyncio.get_running_loop().call_later(0.05, released.set)
            raise GatewayCallError("content policy")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(released.wait(), 1)
        return await real(resolved, prompt, n=n, size=size)

    monkeypatch.setattr(gateway_image, "generate_images", flaky)
    llm.images = SIX_IMAGES
    ctx = _ctx(db, SLIDE_IMAGE_MAX)
    await execute_agent(ctx, "workflow", await _linked_deck(db))

    names = [a["name"] for a in ctx.artifact_keys]
    assert names[0] == "slide-deck.json"
    assert sorted(names[1:]) == [f"slide-image-{n}.png" for n in (2, 5, 6)]
    assert ctx.usage["images"] == 3


async def test_image_failure_keeps_the_deck(db, llm, _mock_runtime_deps, image_model, monkeypatch):
    """이미지 모델 오류는 run을 실패시키지 않는다 — 덱은 저장되고 삽화 단계만 실패로 남는다."""
    from biznotes_core.services.gateway import image as gateway_image
    from biznotes_core.services.gateway.llm import GatewayCallError

    async def failing(resolved, prompt, *, n, size):
        raise GatewayCallError("provider down")

    monkeypatch.setattr(gateway_image, "generate_images", failing)
    llm.images = SIX_IMAGES
    ctx = _ctx(db, SLIDE_IMAGE_MAX)
    await execute_agent(ctx, "workflow", await _linked_deck(db))  # 예외 없이 끝난다
    assert [a["name"] for a in ctx.artifact_keys] == ["slide-deck.json"]
    assert ctx.usage.get("images", 0) == 0


async def test_illustrate_failure_keeps_the_deck(
    db, llm, _mock_runtime_deps, image_model, image_calls, monkeypatch
):
    """묘사 llm 오류도 run을 실패시키지 않는다 — 덱은 이미 저장됐다(묘사·검증의
    `continue_on_error`)."""
    from biznotes_core.services.gateway.llm import GatewayCallError

    real = llm.stream

    async def flaky(db, model_id, messages, result, **kwargs):
        if SlideLLM.kind_of(messages[-1]["content"]) == "illustrate":
            raise GatewayCallError("provider down")
        return await real(db, model_id, messages, result, **kwargs)

    monkeypatch.setattr("biznotes_core.services.gateway.llm.complete_stream_with_fallback", flaky)
    llm.images = SIX_IMAGES
    ctx = _ctx(db, SLIDE_IMAGE_MAX)
    await execute_agent(ctx, "workflow", await _linked_deck(db))  # 예외 없이 끝난다
    assert [a["name"] for a in ctx.artifact_keys] == ["slide-deck.json"]
    assert image_calls == []


# ---------- GET /models?modality=image (D4·AC-11) ----------


async def test_models_modality_requires_login(client: httpx.AsyncClient):
    res = await client.get("/api/v1/models", params={"modality": "image"})
    assert res.status_code == 401


async def test_models_modality_rejects_unknown_value(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    for value in ("embedding", "IMAGE", ""):
        res = await client.get("/api/v1/models", params={"modality": value}, headers=bearer(token))
        assert res.status_code == 422, value


async def test_models_modality_image_lists_enabled_image_models_only(
    client: httpx.AsyncClient, db, user_a, image_model
):
    disabled = LLMModel(
        provider_id=image_model.provider_id,
        model_name="mock-image-off",
        display_name="Mock Image Off",
        capabilities=["image"],
        enabled=False,
    )
    db.add(disabled)
    await db.commit()
    token = await login(client, user_a.email)

    res = await client.get("/api/v1/models", params={"modality": "image"}, headers=bearer(token))
    assert res.status_code == 200
    ids = {m["id"] for m in res.json()}
    assert str(image_model.id) in ids
    assert str(disabled.id) not in ids
    [row] = [m for m in res.json() if m["id"] == str(image_model.id)]
    assert set(row) == {"id", "display_name", "provider_name", "provider_type", "is_default"}

    # 기본값은 지금처럼 채팅 LLM이다 — 이미지 모델이 채팅 선택에 섞이지 않는다
    res = await client.get("/api/v1/models", headers=bearer(token))
    assert res.status_code == 200
    assert str(image_model.id) not in {m["id"] for m in res.json()}


# ---------- 시드 실행(make seed) ----------


async def test_upsert_links_deck_loop_to_body_agent_idempotently():
    from biznotes_core.core.db import async_session_factory
    from sqlalchemy import select

    from scripts.seed import upsert_agents

    async def rows() -> dict[str, Agent]:
        async with async_session_factory() as s:
            found = (
                await s.execute(
                    select(Agent).where(
                        Agent.name.in_([SLIDE_DECK_AGENT_NAME, SLIDE_IMAGE_AGENT_NAME]),
                        Agent.scope == "system",
                        Agent.source_kind == "legacy",
                    )
                )
            ).scalars()
            return {a.name: a for a in found}

    await upsert_agents()
    first = await rows()
    body = first[SLIDE_IMAGE_AGENT_NAME]
    assert (body.kind, body.enabled) == ("workflow", True)
    loop = _nodes(first[SLIDE_DECK_AGENT_NAME].definition)["images"]
    assert loop["loop_body_agent_id"] == str(body.id)

    await upsert_agents()  # 재실행은 같은 행을 다시 잇는다(새 본문을 만들지 않는다)
    second = await rows()
    assert second[SLIDE_IMAGE_AGENT_NAME].id == body.id
    assert _nodes(second[SLIDE_DECK_AGENT_NAME].definition)["images"]["loop_body_agent_id"] == (
        str(body.id)
    )


async def _reset_mock_image(s) -> None:
    """mock 프로바이더의 이미지 행을 지우고 모든 이미지 행의 기본 표시를 내린다(공유 테스트 DB)."""
    from sqlalchemy import any_, delete, literal, select, update

    mock_ids = select(LLMProvider.id).where(LLMProvider.name == "mock").scalar_subquery()
    image = literal("image") == any_(LLMModel.capabilities)
    await s.execute(delete(LLMModel).where(LLMModel.provider_id == mock_ids, image))
    await s.execute(update(LLMModel).where(image).values(is_default=False))
    await s.commit()


async def _seeded_mock_image() -> LLMModel:
    from biznotes_core.core.db import async_session_factory
    from sqlalchemy import any_, literal, select

    async with async_session_factory() as s:
        found = (
            await s.execute(
                select(LLMModel, LLMProvider)
                .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                .where(LLMProvider.name == "mock", literal("image") == any_(LLMModel.capabilities))
            )
        ).all()
    [(model, provider)] = found
    assert (provider.type, model.model_name) == ("mock", "mock-image")
    return model


async def test_upsert_llm_seed_registers_mock_image_model():
    from biznotes_core.core.db import async_session_factory

    from scripts.seed import upsert_llm_seed

    await upsert_llm_seed()  # mock 프로바이더가 없으면 만든다
    async with async_session_factory() as s:
        await _reset_mock_image(s)
    await upsert_llm_seed()
    model = await _seeded_mock_image()
    assert model.capabilities == ["image"]
    assert model.enabled is True
    assert model.is_default is True  # 기본 이미지 모델이 없으면 mock이 기본이다


async def test_upsert_llm_seed_keeps_existing_default_image_model():
    """관리자가 정한 기본 이미지 모델이 있으면 mock은 기본이 아니다 — 부분 유니크 인덱스와 충돌하지
    않는다(PR-2 리뷰 H1)."""
    from biznotes_core.core.db import async_session_factory

    from scripts.seed import upsert_llm_seed

    await upsert_llm_seed()
    async with async_session_factory() as s:
        await _reset_mock_image(s)
        provider = LLMProvider(name=f"img-{uuid.uuid4().hex[:6]}", type="openai", enabled=True)
        s.add(provider)
        await s.flush()
        admin_default = LLMModel(
            provider_id=provider.id,
            model_name="gpt-image-1",
            display_name="관리자 기본 이미지",
            capabilities=["image"],
            is_default=True,
            enabled=True,
        )
        s.add(admin_default)
        await s.commit()
        admin_id = admin_default.id

    await upsert_llm_seed()
    assert (await _seeded_mock_image()).is_default is False
    async with async_session_factory() as s:
        assert (await s.get(LLMModel, admin_id)).is_default is True
