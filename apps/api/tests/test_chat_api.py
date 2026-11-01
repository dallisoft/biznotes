"""채팅 API 테스트 — mock LLM 프로바이더 + 검색 모킹 (임베딩 모델 다운로드 회피)."""

import json
import os
import pathlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from biznotes_core.core.crypto import decrypt, encrypt
from biznotes_core.models import ChatMessage, ChatSession, MCPServer
from biznotes_core.services import rag_chat, usage_guard
from biznotes_core.services.agents.mcp_exposure import set_approved_tools
from biznotes_core.services.agents.mcp_tool_spec import mcp_tool_spec_hash
from biznotes_core.services.rag_chat import Citation, build_messages, filter_cited
from sqlalchemy import func, select, update

from app.core.exception_handlers import RERANK_UNAVAILABLE_DETAIL
from tests.conftest import bearer, login


def test_crypto_roundtrip():
    secret = "sk-test-api-key-12345"
    token = encrypt(secret)
    assert token != secret
    assert decrypt(token) == secret
    # 같은 평문이라도 nonce가 달라 토큰이 달라짐
    assert encrypt(secret) != token


def test_build_messages_numbers_context():
    cits = [
        Citation(
            n=1, source_id="s1", chunk_id="c1", quote="에스프레소는 9기압", source_title="커피"
        ),
        Citation(n=2, source_id="s2", chunk_id="c2", quote="녹차는 70도", source_title="차"),
    ]
    msgs = build_messages("추출 압력은?", cits, history=[])
    assert msgs[0]["role"] == "system"
    assert "[1] (커피)" in msgs[0]["content"]
    assert "[2] (차)" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "추출 압력은?"}


def test_filter_cited_keeps_only_used():
    cits = [
        Citation(n=1, source_id="s1", chunk_id="c1", quote="a", source_title="t1"),
        Citation(n=2, source_id="s2", chunk_id="c2", quote="b", source_title="t2"),
        Citation(n=3, source_id="s3", chunk_id="c3", quote="c", source_title="t3"),
    ]
    used = filter_cited(cits, "답변입니다 [1][3]")
    assert [c.n for c in used] == [1, 3]
    # 마커 없으면 전체 유지
    assert len(filter_cited(cits, "마커 없는 답변")) == 3


@pytest.fixture
async def llm_seed(db):
    from biznotes_core.models import LLMModel, LLMProvider

    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    model = LLMModel(
        provider_id=provider.id,
        model_name="mock-rag",
        display_name="Mock",
        capabilities=["llm"],
        is_default=True,
        enabled=True,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


@pytest.fixture(autouse=True)
def _mock_retrieval(monkeypatch):
    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(
            citations=[
                Citation(
                    n=1,
                    source_id=str(uuid.uuid4()),
                    chunk_id=str(uuid.uuid4()),
                    quote="에스프레소는 9기압의 압력으로 25~30초간 추출한다.",
                    source_title="커피 추출 노트",
                )
            ],
            insufficient_context=False,
        )

    monkeypatch.setattr("app.api.v1.chat.retrieve_context", fake_retrieve)


async def _setup_notebook_session(client, token) -> tuple[str, str]:
    nb = await client.post("/api/v1/notebooks", json={"title": "chat-nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]
    sess = await client.post(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token))
    return nb_id, sess.json()["id"]


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def test_models_endpoint(client: httpx.AsyncClient, user_a, llm_seed):
    token = await login(client, user_a.email)
    res = await client.get("/api/v1/models", headers=bearer(token))
    assert res.status_code == 200
    models = res.json()
    assert any(m["provider_type"] == "mock" for m in models)


async def test_chat_stream_full_flow(client: httpx.AsyncClient, user_a, llm_seed):
    token = await login(client, user_a.email)
    nb_id, session_id = await _setup_notebook_session(client, token)

    res = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "에스프레소 추출 압력은?"},
        headers=bearer(token),
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(res.text)
    types = [e["type"] for e in events]
    assert types[0] == "retrieval"
    assert "delta" in types
    assert types[-1] == "done"

    done = events[-1]
    assert done["citations"][0]["source_title"] == "커피 추출 노트"
    assert done["token_usage"]["total_tokens"] > 0

    # 답변에 인용 마커 포함 (mock은 [1] 마커를 생성)
    answer = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "[1]" in answer

    # 메시지 영속화 확인
    res = await client.get(f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token))
    msgs = res.json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["citations"]

    # 세션 제목이 첫 질문으로 갱신
    res = await client.get(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token))
    assert res.json()[0]["title"].startswith("에스프레소")


async def test_chat_messages_and_history_follow_seq_when_timestamps_tie(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    """seq is the canonical order even after an UPDATE perturbs PostgreSQL heap order."""
    from app.api.v1 import chat as chat_api

    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)
    session_uuid = uuid.UUID(session_id)
    question = ChatMessage(session_id=session_uuid, role="user", content="첫 질문")
    answer = ChatMessage(session_id=session_uuid, role="assistant", content="첫 답변")
    db.add_all([question, answer])
    await db.flush()
    assert question.created_at == answer.created_at
    assert question.seq < answer.seq
    question_id = question.id
    await db.commit()

    # PostgreSQL UPDATE creates a new tuple version after the assistant row. Because created_at
    # remains tied, a created_at-only query can return the assistant first despite seq.
    await db.execute(
        update(ChatMessage).where(ChatMessage.id == question_id).values(content="첫 질문 수정")
    )
    await db.commit()

    listed = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    assert listed.status_code == 200
    assert [message["role"] for message in listed.json()] == ["user", "assistant"]

    captured_history: list[str] = []
    real_build_messages = chat_api.build_messages

    def capture_build_messages(content, citations, history, **kwargs):
        captured_history.extend(message.role for message in history)
        return real_build_messages(content, citations, history, **kwargs)

    monkeypatch.setattr(chat_api, "build_messages", capture_build_messages)
    sent = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "둘째 질문"},
        headers=bearer(token),
    )
    assert sent.status_code == 200
    assert captured_history == ["user", "assistant"]


async def test_chat_insufficient_context_flag_streamed(
    client: httpx.AsyncClient, user_a, llm_seed, monkeypatch
):
    """[Phase 18b] 검색 결과가 근거 부족으로 판정되면 retrieval/done 이벤트에
    insufficient_context:true가 실리고, UI가 배너를 띄울 수 있게 한다."""
    from biznotes_core.services.rag_chat import RetrievalResult

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("app.api.v1.chat.retrieve_context", fake_retrieve)

    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    res = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "근거 없는 질문입니다"},
        headers=bearer(token),
    )
    assert res.status_code == 200
    events = _parse_sse(res.text)
    retrieval = next(e for e in events if e["type"] == "retrieval")
    done = next(e for e in events if e["type"] == "done")
    assert retrieval["insufficient_context"] is True
    assert done["insufficient_context"] is True

    # [codex D3] 신규 컬럼 없이 기존 token_usage(JSONB)에 실려 재로드 후에도 복원 가능해야 함
    res = await client.get(f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token))
    assistant_msg = next(m for m in res.json() if m["role"] == "assistant")
    assert assistant_msg["token_usage"]["insufficient_context"] == 1


async def test_chat_history_passed(client: httpx.AsyncClient, user_a, llm_seed):
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    for q in ("첫 질문", "둘째 질문"):
        res = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": q},
            headers=bearer(token),
        )
        assert res.status_code == 200

    res = await client.get(f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token))
    assert len(res.json()) == 4


# ---------- 리랭커 불가 → 정적 채팅 SSE 고정 문구 (core-v0.15.0 AC-2.1) ----------


@dataclass
class _Point:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


def _use_real_retrieval(monkeypatch, texts: list[str]) -> None:
    """autouse `_mock_retrieval`을 되돌려 코어 `retrieve_context`를 그대로 쓴다.

    검색 경계(`search_spaces`)만 스텁한다 — 리랭커 해석(`resolve_reranker`)은 후보가 있을
    때만 일어나므로 후보를 돌려준다(`test_rag_quality.py` 관례).
    """
    monkeypatch.setattr("app.api.v1.chat.retrieve_context", rag_chat.retrieve_context)
    source_id = str(uuid.uuid4())
    points = [
        _Point(id=str(uuid.uuid4()), score=0.9, payload={"source_id": source_id, "text": text})
        for text in texts
    ]

    async def search(db: Any, spaces: Any, query: str, *, limit: int = 8, **kw: Any) -> list[Any]:
        return points[:limit]

    monkeypatch.setattr("biznotes_core.services.embedding_spaces.search_spaces", search)


def _spy_record_usage(monkeypatch) -> list[tuple[Any, ...]]:
    """관찰용 스파이 — 원 함수를 그대로 부르고 호출만 기록한다."""
    original = usage_guard.record_usage
    calls: list[tuple[Any, ...]] = []

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(usage_guard, "record_usage", spy)
    return calls


async def _message_count(db, session_id: str) -> int:
    stmt = select(func.count()).select_from(ChatMessage)
    stmt = stmt.where(ChatMessage.session_id == uuid.UUID(session_id))
    return (await db.execute(stmt)).scalar_one()


async def test_static_chat_rerank_unavailable_streams_fixed_error(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    """리랭크가 켜졌는데 설정된 게이트웨이 리랭커가 없으면 SSE `error` 1건(고정 문구)으로 끝난다.

    코어 v0.15.0은 리랭크 실패를 하이브리드 점수로 넘기지 않고 `RerankUnavailableError`를
    낸다(#209). 해석 경로는 mock 없이 실제 `resolve_reranker`가 돈다. 메시지·사용량은 남지 않는다.
    """
    from biznotes_core.services.settings_store import set_setting

    _use_real_retrieval(monkeypatch, ["에스프레소는 9기압의 압력으로 25~30초간 추출한다."])
    usage_calls = _spy_record_usage(monkeypatch)
    missing_id = uuid.uuid4()
    await set_setting(
        db, "rag", {"rerank_enabled": True, "rerank_model": f"gateway:{missing_id}:missing"}
    )
    await db.commit()

    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)
    res = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "에스프레소 추출 압력은?"},
        headers=bearer(token),
    )

    assert res.status_code == 200
    events = _parse_sse(res.text)
    assert [e["type"] for e in events] == ["error"]
    assert events[0]["detail"] == RERANK_UNAVAILABLE_DETAIL
    # 코어 예외 문구(「설정된 리랭커가 없습니다(삭제·이름 변경): missing」)는 내보내지 않는다
    assert str(missing_id) not in res.text
    assert "missing" not in res.text
    assert await _message_count(db, session_id) == 0
    assert usage_calls == []


async def test_static_chat_real_retrieval_without_rerank_completes(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    """회귀: 리랭크 꺼짐(기본값, `rag` 행 없음)이면 실제 검색 경로가 retrieval → delta → done."""
    _use_real_retrieval(monkeypatch, ["에스프레소는 9기압의 압력으로 25~30초간 추출한다."])

    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)
    res = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "에스프레소 추출 압력은?"},
        headers=bearer(token),
    )

    assert res.status_code == 200
    types = [e["type"] for e in _parse_sse(res.text)]
    assert types[0] == "retrieval"
    assert "delta" in types
    assert types[-1] == "done"
    assert "error" not in types
    assert await _message_count(db, session_id) == 2


async def test_chat_autonomous_mode_runs_react(
    client: httpx.AsyncClient, user_a, llm_seed, monkeypatch
):
    """[Phase 3] autonomous=true → react_agent 라우팅(옵션 B).

    SSE 계약(retrieval→delta→done)을 유지하며 react 루프가 실제로 돌고,
    응답이 user/assistant 메시지로 영속화되며 done에 autonomous=true가 실린다.
    react는 rag_chat.retrieve_context(rag_search 도구)를 쓰므로 그 경로를 모킹한다.
    """

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)

    token = await login(client, user_a.email)
    nb_id, session_id = await _setup_notebook_session(client, token)

    res = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "커피 추출 과정을 조사해줘", "autonomous": True},
        headers=bearer(token),
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(res.text)
    types = [e["type"] for e in events]
    assert types[0] == "retrieval"
    assert "delta" in types
    assert types[-1] == "done"

    done = events[-1]
    assert done["autonomous"] is True
    assert done["citations"] == []  # 자율 모드는 사전 검색 인용을 비운다
    assert done["token_usage"]["total_tokens"] > 0
    assert done["tool_calls"][0]["tool"] == "rag_search"
    assert done["tool_calls"][0]["status"] == "succeeded"
    assert done["evidence"] == []

    # user + assistant 메시지 영속화
    res = await client.get(f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token))
    msgs = res.json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"]


async def test_autonomous_message_persists_provenance(
    client: httpx.AsyncClient, user_a, llm_seed, monkeypatch
):
    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "근거를 조사해줘", "autonomous": True},
        headers=bearer(token),
    )
    assert response.status_code == 200

    messages = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    assistant = next(message for message in messages.json() if message["role"] == "assistant")
    assert assistant["provenance"]["v"] == 1
    assert assistant["provenance"]["mode"] == "autonomous"
    assert assistant["provenance"]["items"][0] == {
        "kind": "tool",
        "status": "succeeded",
        "name": None,
        "version": None,
        "server": None,
        "tool": "rag_search",
        "source_id": None,
        "title": None,
        "count": None,
    }


async def test_autonomous_run_skill_sse_evidence_uses_public_keys(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    from biznotes_core.models import Agent, Skill, SkillVersion
    from biznotes_core.services.agents import runtime
    from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

    skill = Skill(
        name="공개 근거 스킬",
        description="SSE 공개 키 검증",
        slug=f"public-evidence-{uuid.uuid4().hex[:6]}",
        visibility="system",
        status="published",
        enabled=True,
    )
    db.add(skill)
    await db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version=4,
        package_uri="skills/public-evidence/4",
        skill_md_hash="a" * 64,
        package_hash="b" * 64,
        manifest={"mode": "instruction"},
        file_tree=[],
        security_report={},
        status="published",
        created_by=user_a.id,
    )
    db.add(version)
    await db.flush()
    # 노출 정본은 공개 포인터다(biznotes-core 표준 계획 D8) — status만으로는 노출되지 않는다
    skill.published_version_id = version.id
    wrapper = Agent(
        name=skill.name,
        kind="skill",
        icon="🧭",
        definition={},
        params_schema={},
        scope="system",
        owner_id=None,
        enabled=True,
        source_kind="skill_package",
        skill_version_id=version.id,
    )
    db.add(wrapper)
    await db.commit()

    async def run_subagent(ctx, target_id, params, *, version_id=None):
        assert target_id == str(wrapper.id)
        return "스킬 실행 완료"

    turns = {"count": 0}

    async def complete_with_skill(db_, model_id, messages, tools, **kwargs):
        turns["count"] += 1
        if turns["count"] == 1:
            call = ToolCall(
                id="skill-call",
                name="run_skill",
                arguments={"agent_id": str(wrapper.id), "params": {}},
            )
            return ToolTurnResult(
                content="",
                tool_calls=[call],
                assistant_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "skill-call",
                            "type": "function",
                            "function": {
                                "name": "run_skill",
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                    ],
                },
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                model=None,
            )
        return ToolTurnResult(
            content="완료했습니다.",
            tool_calls=[],
            assistant_message={"role": "assistant", "content": "완료했습니다."},
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model=None,
        )

    monkeypatch.setattr(runtime, "_run_subagent", run_subagent)
    monkeypatch.setattr(
        "biznotes_core.services.gateway.llm.complete_with_tools", complete_with_skill
    )
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "스킬로 근거를 검토해줘", "autonomous": True},
        headers=bearer(token),
    )

    assert response.status_code == 200
    done = next(event for event in _parse_sse(response.text) if event["type"] == "done")
    assert done["evidence"] == [
        {
            "kind": "skill",
            "status": "succeeded",
            "name": skill.name,
            "version": 4,
            "server": None,
            "tool": None,
            "source_id": None,
            "title": None,
            "count": None,
        }
    ]
    assert not {"skill_version_id", "package_hash", "agent_id"} & set(done["evidence"][0])


async def test_static_message_records_source_items(client: httpx.AsyncClient, user_a, llm_seed):
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "에스프레소 추출 압력은?"},
        headers=bearer(token),
    )
    assert response.status_code == 200

    messages = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    assistant = next(message for message in messages.json() if message["role"] == "assistant")
    assert assistant["provenance"]["mode"] == "static"
    assert assistant["provenance"]["items"][0]["kind"] == "source"
    assert assistant["provenance"]["items"][0]["source_id"]
    assert assistant["provenance"]["items"][0]["title"] == "커피 추출 노트"
    assert assistant["provenance"]["items"][0]["status"] == "succeeded"


async def test_legacy_message_without_provenance_serializes(
    client: httpx.AsyncClient, db, user_a, llm_seed
):
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)
    session = await db.get(ChatSession, uuid.UUID(session_id))
    assert session is not None
    db.add(
        ChatMessage(
            session_id=session.id,
            role="assistant",
            content="과거 답변",
            citations=[],
            token_usage={},
            provenance=None,
        )
    )
    await db.commit()

    response = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    assert response.status_code == 200
    assert response.json()[0]["provenance"] is None


async def test_autonomous_mcp_unavailable_recorded_as_failed(
    client: httpx.AsyncClient, db, admin_user, llm_seed, monkeypatch
):
    """승인된 MCP 도구 호출이 공급자 장애로 실패하면 SSE·provenance에 failed evidence가 남는다.

    core-v0.11.0-upgrade AC-1.7 — 예전에는 `build_tool_specs`와 비공개
    `_SAFE_DISCOVERED_MCP_TOOLS`를 monkeypatch했다. 이제 승인 행(정본, #139 B)을 시드하고 코어
    디스커버리·디스패처를 그대로 탄다. 대체는 MCP 경계(`list_tools`·`call_tool`)와 LLM뿐이다.
    MCP는 관리자(admin_only 기본) 실행에서만 노출되므로 admin 사용자로 확인한다.
    """
    from biznotes_core.services.agents.tool_registry import mcp_server_tag
    from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult

    server = MCPServer(
        name="regions",
        transport="streamable_http",
        endpoint_or_command="https://mcp.example/regions",
        enabled=True,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    listed = {"name": "find_region", "description": "지역 조회", "input_schema": {}}
    # 코어 v0.21.0 #293 — 디스커버리는 지금 스펙의 해시가 승인 고정값과 같을 때만 노출한다
    await set_approved_tools(
        db,
        server,
        {"find_region": True},
        admin_id=None,
        spec_hashes={"find_region": mcp_tool_spec_hash(listed)},
    )
    await db.commit()
    exposed_name = f"mcp_{mcp_server_tag(server.id)}_find_region"

    async def fake_list(server_):
        return [listed]

    async def unavailable_call(server_, name, arguments, **_kw):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.list_tools", fake_list)
    monkeypatch.setattr("biznotes_core.services.agents.mcp_client.call_tool", unavailable_call)

    calls = {"count": 0}

    async def fake_complete(db, model_id, messages, tools, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            assert exposed_name in [t["function"]["name"] for t in tools]  # 실제 디스커버리 노출
            call = ToolCall(id="mcp-call", name=exposed_name, arguments={})
            return ToolTurnResult(
                content="",
                tool_calls=[call],
                assistant_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "mcp-call",
                            "type": "function",
                            "function": {"name": exposed_name, "arguments": "{}"},
                        }
                    ],
                },
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                model=None,
            )
        return ToolTurnResult(
            content="조회 실패를 확인했습니다.",
            tool_calls=[],
            assistant_message={"role": "assistant", "content": "조회 실패를 확인했습니다."},
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            model=None,
        )

    monkeypatch.setattr("biznotes_core.services.gateway.llm.complete_with_tools", fake_complete)
    token = await login(client, admin_user.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "지역 정보를 조회해줘", "autonomous": True},
        headers=bearer(token),
    )
    assert response.status_code == 200
    done = next(event for event in _parse_sse(response.text) if event["type"] == "done")
    assert done["tool_calls"][0]["status"] == "failed"
    assert done["evidence"] == [
        {
            "kind": "mcp",
            "status": "failed",
            "name": None,
            "version": None,
            "server": "regions",
            "tool": "find_region",
            "source_id": None,
            "title": None,
            "count": None,
        }
    ]

    messages = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    assistant = next(message for message in messages.json() if message["role"] == "assistant")
    assert assistant["provenance"]["items"][0]["kind"] == "mcp"
    assert assistant["provenance"]["items"][0]["status"] == "failed"
    assert len(assistant["provenance"]["items"]) == 1


async def test_provenance_builder_failure_does_not_block_message_save(
    client: httpx.AsyncClient, user_a, llm_seed, monkeypatch
):
    def fail_builder(*args, **kwargs):
        raise RuntimeError("builder failed")

    monkeypatch.setattr("app.api.v1.chat.build_chat_provenance", fail_builder)
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)
    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "에스프레소 추출 압력은?"},
        headers=bearer(token),
    )
    assert response.status_code == 200

    messages = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    assistant = next(message for message in messages.json() if message["role"] == "assistant")
    assert assistant["provenance"] is None


async def test_autonomous_provenance_builder_failure_saves_null_and_warns(
    client: httpx.AsyncClient, user_a, llm_seed, monkeypatch
):
    from app.api.v1 import chat as chat_api

    warnings: list[tuple[str, dict]] = []

    def fail_builder(*args, **kwargs):
        raise RuntimeError("builder failed")

    class RecordingLogger:
        def warning(self, event, **kwargs):
            warnings.append((event, kwargs))

    monkeypatch.setattr(chat_api, "build_chat_provenance", fail_builder)
    monkeypatch.setattr(chat_api, "logger", RecordingLogger())
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "자율 근거를 조사해줘", "autonomous": True},
        headers=bearer(token),
    )
    assert response.status_code == 200

    messages = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    assistant = next(message for message in messages.json() if message["role"] == "assistant")
    assert assistant["provenance"] is None
    assert warnings == [
        (
            "채팅 provenance 생성 실패",
            {"mode": "autonomous", "error": "builder failed"},
        )
    ]


async def test_chat_autonomous_followup_keeps_history(
    client: httpx.AsyncClient, user_a, llm_seed, monkeypatch
):
    """[Phase 3·codex D2] 자율 모드 후속 턴은 같은 세션 이력을 react에 전달한다.

    두 번째 자율 메시지에서 node['history']에 직전 턴이 실려야 함을 run_react_node
    호출 인자를 가로채 확인한다(맥락 유지). 메시지는 4건으로 누적된다.
    """

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)

    seen_history: list = []
    seen_tools: list = []
    seen_system: list = []
    import biznotes_core.services.agents.runtime as runtime_mod

    real_run = runtime_mod.run_react_node

    async def spy_run(ctx, node, state=None):
        seen_history.append(node.get("history"))
        seen_tools.append(node.get("allowed_tools"))
        seen_system.append(node.get("system"))
        return await real_run(ctx, node, state)

    monkeypatch.setattr("app.api.v1.chat.run_react_node", spy_run, raising=False)
    # chat.py가 함수 내부에서 import하므로 원본 모듈 심볼도 패치.
    monkeypatch.setattr(runtime_mod, "run_react_node", spy_run)

    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    for q in ("커피 추출 방법은?", "두 번째 것 설명해줘"):
        res = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": q, "autonomous": True},
            headers=bearer(token),
        )
        assert res.status_code == 200

    # 첫 턴은 이력 없음(None), 둘째 턴은 직전 user/assistant 이력이 실린다.
    assert seen_history[0] is None
    assert seen_history[1] is not None
    roles = [m["role"] for m in seen_history[1]]
    assert "user" in roles and "assistant" in roles
    # chat-safe 정책은 소스 원문·승인된 MCP·published 시스템 스킬·읽기 전용 산출물 도구만 쓴다
    # (코어 v0.10.12 #133이 artifact_list·artifact_read를 CHAT_SAFE_TOOLS에 더했다 — HCI-12 (a)).
    chat_safe = [
        "rag_search",
        "source_load",
        "source_list",  # 코어 v0.30.0 #447
        "mcp",
        "run_skill",
        "activate_skill",
        "read_skill_file",
        "artifact_list",
        "artifact_read",
    ]
    assert seen_tools == [chat_safe, chat_safe]
    # 소스 도달 규칙(#284) — 목록으로 후보를 찾고 source_ids로 골라 읽고 next_offset으로 이어 읽는다
    from app.api.v1.chat import AUTONOMOUS_SOURCE_RULE

    for system in seen_system:
        assert AUTONOMOUS_SOURCE_RULE in system
    for needle in ("source_list", "source_ids", "next_offset", "읽은 범위"):
        assert needle in AUTONOMOUS_SOURCE_RULE

    res = await client.get(f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token))
    assert len(res.json()) == 4


async def test_chat_toggle_off_uses_static_path(client: httpx.AsyncClient, user_a, llm_seed):
    """[Phase 3] autonomous 생략(기본 False) → 기존 정적 rag_chat 경로(회귀 없음).

    정적 경로는 사전 검색 인용을 retrieval/done에 싣고 인용 마커 [1]을 생성한다
    — 자율 경로(빈 인용)와 구분되는 정적 경로 고유 동작으로 회귀 부재를 입증한다.
    """
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    res = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "에스프레소 추출 압력은?"},  # autonomous 미지정 → 기본 정적
        headers=bearer(token),
    )
    assert res.status_code == 200
    events = _parse_sse(res.text)
    done = events[-1]
    assert done["type"] == "done"
    assert "autonomous" not in done  # 정적 경로는 autonomous 플래그를 싣지 않는다
    assert done["citations"][0]["source_title"] == "커피 추출 노트"  # 사전 검색 인용 유지


async def test_session_isolation(client: httpx.AsyncClient, user_a, user_b, llm_seed):
    token_a = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token_a)

    client.cookies.clear()
    token_b = await login(client, user_b.email)
    res = await client.get(f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token_b))
    assert res.status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# BL-22 / AC-14 — SSE `done.provenance` 단일 진실 공급원 + fixture 계약(PR-16 · RR-02)
#
# fixture는 웹 워크스페이스(`apps/web/src/lib/__fixtures__/sse-done/`)에 있고 웹
# 리듀서 테스트(`sse.test.ts`)가 같은 파일을 소비한다 — 두 계층이 같은 바이트를 보게
# 하려는 의도된 스코프 예외(계획 Phase 4). 기본 실행은 **읽기 전용 대조**이고,
# 재생성은 환경변수 `PYTEST_UPDATE_SSE_FIXTURES=1` opt-in일 때만 일어난다.
# ──────────────────────────────────────────────────────────────────────────────

SSE_DONE_FIXTURE_DIR = (
    pathlib.Path(__file__).resolve().parents[3]
    / "apps"
    / "web"
    / "src"
    / "lib"
    / "__fixtures__"
    / "sse-done"
)

_PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"
_PLACEHOLDER_TS = "2026-01-01T00:00:00Z"
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_TS_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
# 실행마다 달라지는 토큰 카운터만 0으로 고정한다 — fallback/insufficient_context 같은
# 의미 플래그는 계약의 일부라 그대로 둔다.
_VOLATILE_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _normalize_done(value):
    """`done` 페이로드에서 실행마다 달라지는 값만 고정 placeholder로 바꾼다.

    키 집합·중첩 구조·타입은 그대로 남으므로 fixture는 여전히 웹 리듀서가 소비할 수 있는
    유효한 `ChatStreamEvent`다(AC-14 "키 집합 동일 + provenance 형태").
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "token_usage" and isinstance(item, dict):
                out[key] = {
                    k: (0 if k in _VOLATILE_TOKEN_KEYS and isinstance(v, int) else v)
                    for k, v in item.items()
                }
            else:
                out[key] = _normalize_done(item)
        return out
    if isinstance(value, list):
        return [_normalize_done(item) for item in value]
    if isinstance(value, str):
        if _UUID_RE.match(value):
            return _PLACEHOLDER_UUID
        if _TS_RE.match(value):
            return _PLACEHOLDER_TS
    return value


def _fixture_text(normalized) -> str:
    return json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _compare_or_update_fixture(path: pathlib.Path, payload: dict) -> dict:
    """fixture와 `done` 페이로드를 대조한다. 기본은 **읽기 전용**(RR-02).

    `PYTEST_UPDATE_SSE_FIXTURES=1`일 때만 파일을 갱신한다. pytest 옵션을 등록하지 않아
    (`pytest_addoption` 부재) conftest 변경 없이 동작한다.
    """
    normalized = _normalize_done(payload)
    if os.environ.get("PYTEST_UPDATE_SSE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_fixture_text(normalized), encoding="utf-8")
        return normalized
    assert path.exists(), (
        f"SSE done fixture 없음: {path} — PYTEST_UPDATE_SSE_FIXTURES=1로 재생성하세요"
    )
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert normalized == expected, (
        f"SSE done 계약 드리프트: {path.name}\n실제: {_fixture_text(normalized)}"
        f"fixture: {_fixture_text(expected)}"
        "재생성: PYTEST_UPDATE_SSE_FIXTURES=1 uv run pytest tests/test_chat_api.py"
        " -k sse_done_fixture"
    )
    return normalized


async def test_sse_done_fixture_autonomous_matches_stored_provenance(
    client: httpx.AsyncClient, user_a, llm_seed
):
    """자율 `done.provenance` == 재조회 `ChatMessageOut.provenance` + fixture 대조(AC-14)."""
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "커피 추출 과정을 조사해줘", "autonomous": True},
        headers=bearer(token),
    )
    assert response.status_code == 200
    done = _parse_sse(response.text)[-1]
    assert done["type"] == "done"

    messages = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    stored = next(m for m in messages.json() if m["role"] == "assistant")
    # 라이브 == 재조회: 같은 헬퍼(ProvenanceOut)로 직렬화되므로 dict 동등이어야 한다.
    assert done["provenance"] == stored["provenance"]
    assert done["provenance"]["mode"] == "autonomous"
    assert done["message_id"] == stored["id"]

    _compare_or_update_fixture(SSE_DONE_FIXTURE_DIR / "autonomous.json", done)


async def test_sse_done_fixture_static_matches_stored_provenance(
    client: httpx.AsyncClient, user_a, llm_seed
):
    """정적 `done.provenance` == 재조회 provenance + fixture 대조(AC-14 · HCI-P2-6)."""
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "에스프레소 추출 압력은?"},
        headers=bearer(token),
    )
    assert response.status_code == 200
    done = _parse_sse(response.text)[-1]
    assert done["type"] == "done"

    messages = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    stored = next(m for m in messages.json() if m["role"] == "assistant")
    assert done["provenance"] == stored["provenance"]
    assert done["provenance"]["mode"] == "static"
    # HCI-P2-6: 정적 라이브 답변도 소스 배지를 받는다.
    assert done["provenance"]["items"][0]["kind"] == "source"

    # [P4-06] fixture 정규화는 UUID 꼴 문자열을 전부 placeholder로 바꾸므로 식별자 **관계**를
    # 잃는다. 정규화 전 원문에서 provenance 항목이 실제 인용 소스를 가리키는지 고정한다.
    assert done["provenance"]["items"][0]["source_id"] == done["citations"][0]["source_id"]
    assert done["provenance"]["items"][0]["title"] == done["citations"][0]["source_title"]
    assert done["message_id"] == stored["id"]

    _compare_or_update_fixture(SSE_DONE_FIXTURE_DIR / "static.json", done)


def test_sse_done_fixture_legacy_models_pre_provenance_api():
    """`legacy.json`은 구 API 응답 모델 — `provenance` 키 부재 + 두 deprecated 필드 존재."""
    legacy = json.loads((SSE_DONE_FIXTURE_DIR / "legacy.json").read_text(encoding="utf-8"))
    assert legacy["type"] == "done"
    assert "provenance" not in legacy
    assert legacy["tool_calls"], "legacy fixture는 tool_calls를 가져야 한다"
    assert isinstance(legacy["evidence"], list)
    # [P4-07] `autonomous.json`은 실코드에서 재생성되므로 부분집합 단언은 `done`에 키가
    # 늘어날수록 느슨해지기만 한다. 구 계약의 키 집합을 **명시 리터럴**로 고정한다.
    assert set(legacy) == {
        "type",
        "message_id",
        "citations",
        "token_usage",
        "model",
        "fallback_used",
        "autonomous",
        "tool_calls",
        "evidence",
    }
    # 그 위에서 구 계약이 새 계약의 부분집합임도 확인한다(세 필드 optional 모델링의 근거).
    autonomous = json.loads(
        (SSE_DONE_FIXTURE_DIR / "autonomous.json").read_text(encoding="utf-8")
    )
    assert set(legacy) <= set(autonomous)


async def test_sse_done_provenance_null_when_builder_fails(
    client: httpx.AsyncClient, user_a, llm_seed, monkeypatch
):
    """빌더가 실패하면 저장·SSE 모두 provenance가 null이다(AC-14) — 답변은 그대로 전달."""

    def boom(*args, **kwargs):
        raise RuntimeError("provenance 빌더 강제 실패")

    monkeypatch.setattr("app.api.v1.chat.build_chat_provenance", boom)
    token = await login(client, user_a.email)
    _, session_id = await _setup_notebook_session(client, token)

    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "빌더 실패 시 동작", "autonomous": True},
        headers=bearer(token),
    )
    assert response.status_code == 200
    done = _parse_sse(response.text)[-1]
    assert done["type"] == "done"
    assert done["provenance"] is None
    assert done["message_id"]  # 답변 저장은 계속된다

    messages = await client.get(
        f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token)
    )
    stored = next(m for m in messages.json() if m["role"] == "assistant")
    assert stored["provenance"] is None
    assert done["provenance"] == stored["provenance"]


def test_compare_or_update_fixture_is_read_only_by_default(tmp_path, monkeypatch):
    """env 미설정: 불일치면 AssertionError이고 파일 바이트는 불변이다(AC-14 읽기 전용 성질)."""
    monkeypatch.delenv("PYTEST_UPDATE_SSE_FIXTURES", raising=False)
    target = tmp_path / "autonomous.json"
    original = (SSE_DONE_FIXTURE_DIR / "autonomous.json").read_bytes()
    target.write_bytes(original)

    with pytest.raises(AssertionError):
        _compare_or_update_fixture(target, {"type": "done", "model": "드리프트"})

    assert target.read_bytes() == original


def test_compare_or_update_fixture_writes_only_with_env_opt_in(tmp_path, monkeypatch):
    """env=1: 불일치 페이로드로 파일이 갱신된다(AC-14 opt-in 재생성)."""
    monkeypatch.setenv("PYTEST_UPDATE_SSE_FIXTURES", "1")
    target = tmp_path / "autonomous.json"
    original = (SSE_DONE_FIXTURE_DIR / "autonomous.json").read_bytes()
    target.write_bytes(original)

    payload = {"type": "done", "message_id": str(uuid.uuid4()), "model": "드리프트"}
    _compare_or_update_fixture(target, payload)

    assert target.read_bytes() != original
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "type": "done",
        "message_id": _PLACEHOLDER_UUID,
        "model": "드리프트",
    }
