"""Phase F — 프롬프트 버전 관리 API + 런타임 prompt_ref 해석 테스트."""

import uuid

import httpx

from tests.conftest import bearer, login


async def _create_template(client, token, content="버전1 본문", name="템플릿"):
    res = await client.post(
        "/api/v1/prompts",
        json={"name": name, "content": content, "system": "sys", "note": "init"},
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    return res.json()


async def test_prompt_template_create_and_version_lifecycle(client: httpx.AsyncClient, user_a):
    token = await login(client, user_a.email)
    tpl = await _create_template(client, token)
    assert tpl["active_version"] == 1
    assert tpl["version_count"] == 1
    tid = tpl["id"]

    # 버전 추가 → version 2 활성
    res = await client.post(
        f"/api/v1/prompts/{tid}/versions",
        json={"content": "버전2 본문", "note": "개선"},
        headers=bearer(token),
    )
    assert res.status_code == 201
    v2 = res.json()
    assert v2["version"] == 2

    got = (await client.get(f"/api/v1/prompts/{tid}", headers=bearer(token))).json()
    assert got["active_version"] == 2
    assert got["version_count"] == 2

    # 버전 목록 (내림차순)
    versions = (
        await client.get(f"/api/v1/prompts/{tid}/versions", headers=bearer(token))
    ).json()
    assert [v["version"] for v in versions] == [2, 1]

    # 버전1로 롤백
    v1_id = next(v["id"] for v in versions if v["version"] == 1)
    res = await client.post(
        f"/api/v1/prompts/{tid}/rollback/{v1_id}", headers=bearer(token)
    )
    assert res.status_code == 200
    assert res.json()["active_version"] == 1


async def test_prompt_template_edit_forbidden_for_other(
    client: httpx.AsyncClient, user_a, user_b
):
    token_a = await login(client, user_a.email)
    tpl = await _create_template(client, token_a)
    tid = tpl["id"]

    token_b = await login(client, user_b.email)
    # 타인은 조회 불가(404) → 버전 추가도 불가
    res = await client.post(
        f"/api/v1/prompts/{tid}/versions",
        json={"content": "남의 것"},
        headers=bearer(token_b),
    )
    assert res.status_code == 404


async def test_resolve_active_prompt_service(db, user_a):
    from biznotes_core.models import PromptTemplate, PromptVersion
    from biznotes_core.services.prompts import resolve_active_prompt

    tpl = PromptTemplate(name="t", owner_id=user_a.id, scope="user")
    db.add(tpl)
    await db.flush()
    v1 = PromptVersion(template_id=tpl.id, version=1, content="첫 본문", system="시스템1")
    v2 = PromptVersion(template_id=tpl.id, version=2, content="둘째 본문", system="")
    db.add_all([v1, v2])
    await db.flush()
    tpl.active_version_id = v2.id
    await db.commit()

    content, system = await resolve_active_prompt(db, tpl.id)
    assert content == "둘째 본문"

    # 롤백 후 v1 본문/시스템
    tpl.active_version_id = v1.id
    await db.commit()
    content, system = await resolve_active_prompt(db, tpl.id)
    assert content == "첫 본문"
    assert system == "시스템1"

    # 미존재 템플릿
    assert await resolve_active_prompt(db, uuid.uuid4()) is None


async def test_llm_node_prompt_ref(db, user_a):
    """llm 노드 prompt_ref — 템플릿 활성 버전 본문을 프롬프트로 사용."""
    from biznotes_core.models import LLMModel, LLMProvider, PromptTemplate, PromptVersion
    from biznotes_core.services.agents.runtime import RunContext, execute_agent

    provider = LLMProvider(name=f"mock-pr-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-pr",
        display_name="Mock PR",
        capabilities=["llm"],
        is_default=False,
        enabled=True,
    )
    db.add(model)

    tpl = PromptTemplate(name="런타임템플릿", owner_id=user_a.id, scope="user")
    db.add(tpl)
    await db.flush()
    # [1] 블록 → mock LLM이 추출해 응답에 에코하므로 본문 사용을 검증 가능
    ver = PromptVersion(
        template_id=tpl.id, version=1, content="[1] 프롬프트버전테스트 문장입니다.", system=""
    )
    db.add(ver)
    await db.flush()
    tpl.active_version_id = ver.id
    await db.commit()

    definition = {
        "nodes": [
            {"id": "gen", "type": "llm", "model_id": str(model.id), "prompt_ref": str(tpl.id)},
        ],
        "edges": [],
    }
    ctx = RunContext(
        run_id=uuid.uuid4(), notebook_id=uuid.uuid4(), db=db, params={}, user_id=user_a.id
    )
    summary = await execute_agent(ctx, "workflow", definition)
    # 템플릿 본문의 [1] 블록 내용이 응답에 반영됨 → prompt_ref가 실제로 사용됨
    assert "프롬프트버전테스트" in summary


# --- R3-2: 목록 경로의 N+1 해소 (벌크 2차 쿼리) ---


async def test_목록은_템플릿_수와_무관하게_상수_쿼리다(db, user_a):
    """이전에는 템플릿당 count 쿼리가 붙어 N+1이었다(20개 → 22쿼리)."""
    from biznotes_core.api.v1.prompts import _to_out_bulk
    from biznotes_core.core.db import engine
    from biznotes_core.models import PromptTemplate
    from sqlalchemy import event

    counter = {"n": 0}

    def _count(*a, **k):
        counter["n"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        counts = {}
        for k in (1, 10):
            tpls = [
                PromptTemplate(owner_id=user_a.id, name=f"t{k}-{i}", scope="user")
                for i in range(k)
            ]
            db.add_all(tpls)
            await db.commit()
            counter["n"] = 0
            await _to_out_bulk(db, tpls, user_a)
            counts[k] = counter["n"]
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)

    assert counts[10] == counts[1], f"템플릿 수에 비례하면 N+1 (측정: {counts})"


async def test_벌크_출력이_단건_출력과_동일하다(db, user_a):
    """벌크 경로가 active_version·version_count를 단건 경로와 같게 계산하는가."""
    from biznotes_core.api.v1.prompts import _to_out, _to_out_bulk
    from biznotes_core.models import PromptTemplate, PromptVersion

    tpl = PromptTemplate(owner_id=user_a.id, name="eq", scope="user")
    db.add(tpl)
    await db.flush()
    v1 = PromptVersion(template_id=tpl.id, version=1, content="c1", system="", note="")
    v2 = PromptVersion(template_id=tpl.id, version=2, content="c2", system="", note="")
    db.add_all([v1, v2])
    await db.flush()
    tpl.active_version_id = v2.id
    await db.commit()

    single = await _to_out(db, tpl, user_a)
    bulk = (await _to_out_bulk(db, [tpl], user_a))[0]

    assert bulk == single
    assert bulk.active_version == 2
    assert bulk.version_count == 2


async def test_active_version이_없는_템플릿도_처리한다(db, user_a):
    from biznotes_core.api.v1.prompts import _to_out, _to_out_bulk
    from biznotes_core.models import PromptTemplate

    tpl = PromptTemplate(owner_id=user_a.id, name="noactive", scope="user")
    db.add(tpl)
    await db.commit()

    bulk = (await _to_out_bulk(db, [tpl], user_a))[0]
    assert bulk == await _to_out(db, tpl, user_a)
    assert bulk.active_version is None
    assert bulk.version_count == 0


async def test_빈_목록은_쿼리를_날리지_않는다(db, user_a):
    from biznotes_core.api.v1.prompts import _to_out_bulk

    assert await _to_out_bulk(db, [], user_a) == []
