"""자율 채팅 답변의 인용 citations(#161).

웹 채팅은 모든 질문을 자율 모드로 보낸다. 코어 `rag_search` 도구는 run 단위 번호를 매긴 발췌를
`ctx.tool_evidence`(`kind="source"`, `citation`)에 남긴다. 자율 경로는 그 근거표에서 답변이
실제로 인용한 `[n]`만 골라 검증하고, run 노트북 소속이 확인된 것만 `done`·저장 메시지에 싣는다.

LLM(`complete_with_tools`)과 검색 경계(`retrieve_context`)만 스텁한다 — 근거표 기록
(`_record_run_citations`)·인용 선별·검증·소속 확인은 실제 코드가 돈다.
"""

import json
import re
import uuid
from typing import Any

import httpx
import pytest
from biznotes_core.services import rag_chat
from biznotes_core.services.gateway.llm import ToolCall, ToolTurnResult
from biznotes_core.services.rag_chat import Citation, RetrievalResult

from app.api.v1.chat import AUTONOMOUS_CITATION_RULE, AUTONOMOUS_CITATION_SCOPE
from tests.conftest import bearer, login

QUOTE_OWN = "에스프레소는 9기압의 압력으로 25~30초간 추출한다."
QUOTE_OWN_2 = "드립 커피는 92도 물로 천천히 내린다."
QUOTE_FOREIGN = "다른 노트북의 기밀 발췌 문장이다."


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


async def _notebook_with_session(client, token) -> tuple[str, str]:
    nb = await client.post("/api/v1/notebooks", json={"title": "cite-nb"}, headers=bearer(token))
    nb_id = nb.json()["id"]
    sess = await client.post(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token))
    return nb_id, sess.json()["id"]


async def _add_chunk(db, notebook_id: str, text: str) -> tuple[str, str]:
    """소스·청크를 만들고 `(source_id, 인용 chunk_id)`를 돌려준다 — chunk_id는 청크 행 id다."""
    from biznotes_core.models import Source, SourceChunk

    source = Source(notebook_id=uuid.UUID(notebook_id), type="text", title="소스", status="ready")
    db.add(source)
    await db.flush()
    chunk = SourceChunk(source_id=source.id, seq=0, text=text)
    db.add(chunk)
    await db.commit()
    return str(source.id), str(chunk.id)


def _stub_retrieval(monkeypatch, pairs: list[tuple[str, str, str]]) -> None:
    """코어 `rag_search` 도구가 부르는 검색 경계 — 매 호출 [1]부터 번호를 매긴다(코어와 같다)."""

    async def fake_retrieve(db, notebook_id, query, source_ids=None, top_k=6):
        return RetrievalResult(
            citations=[
                Citation(n=i, source_id=sid, chunk_id=cid, quote=quote, source_title="소스")
                for i, (sid, cid, quote) in enumerate(pairs, start=1)
            ],
            insufficient_context=False,
        )

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)


def _stub_llm(monkeypatch, answer: str) -> list[list[dict[str, Any]]]:
    """1턴: rag_search 호출, 2턴: `answer`로 마무리. 받은 messages를 기록한다."""
    seen: list[list[dict[str, Any]]] = []

    async def fake_complete(db_, model_id, messages, tools, **kwargs):
        seen.append(list(messages))
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if len(seen) == 1:
            call = ToolCall(id="rag-1", name="rag_search", arguments={"query": "커피"})
            return ToolTurnResult(
                content="",
                tool_calls=[call],
                assistant_message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "rag-1",
                            "type": "function",
                            "function": {
                                "name": "rag_search",
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                    ],
                },
                usage=usage,
                model=None,
            )
        return ToolTurnResult(
            content=answer,
            tool_calls=[],
            assistant_message={"role": "assistant", "content": answer},
            usage=usage,
            model=None,
        )

    monkeypatch.setattr("biznotes_core.services.gateway.llm.complete_with_tools", fake_complete)
    return seen


def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def _ask(client, token, session_id) -> tuple[dict, dict]:
    res = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "커피 추출을 알려줘", "autonomous": True},
        headers=bearer(token),
    )
    assert res.status_code == 200
    events = _parse_sse(res.text)
    assert "error" not in [e["type"] for e in events], events
    done = events[-1]
    assert done["type"] == "done"
    msgs = await client.get(f"/api/v1/chat/sessions/{session_id}/messages", headers=bearer(token))
    assistant = next(m for m in msgs.json() if m["role"] == "assistant")
    return done, assistant


async def test_autonomous_done_and_saved_message_carry_cited_sources(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    """AC-1: 답변이 인용한 [n]만 done.citations와 저장 메시지에 실린다(미인용 발췌 제외)."""
    token = await login(client, user_a.email)
    nb_id, session_id = await _notebook_with_session(client, token)
    sid1, cid1 = await _add_chunk(db, nb_id, QUOTE_OWN)
    sid2, cid2 = await _add_chunk(db, nb_id, QUOTE_OWN_2)
    _stub_retrieval(monkeypatch, [(sid1, cid1, QUOTE_OWN), (sid2, cid2, QUOTE_OWN_2)])
    _stub_llm(monkeypatch, "에스프레소는 9기압의 압력으로 25~30초간 추출합니다 [1].")

    done, assistant = await _ask(client, token, session_id)

    expected = [
        {
            "n": 1,
            "source_id": sid1,
            "chunk_id": cid1,
            "quote": QUOTE_OWN,
            "source_title": "소스",
            "verified": True,
        }
    ]
    assert done["citations"] == expected
    assert assistant["citations"] == expected
    assert "[1]" in assistant["content"]


async def test_autonomous_citations_drop_sources_outside_run_notebook(
    client: httpx.AsyncClient, db, user_a, user_b, llm_seed, monkeypatch
):
    """AC-2(§3.3 비인가): 다른 사용자 노트북의 소스·청크를 가리키는 인용은 버린다.

    소스는 자기 노트북 것이지만 청크가 타인 노트북 것인 뒤섞인 쌍도 버린다.
    """
    token_a = await login(client, user_a.email)
    nb_a, session_id = await _notebook_with_session(client, token_a)
    token_b = await login(client, user_b.email)
    nb_b, _ = await _notebook_with_session(client, token_b)
    sid_own, cid_own = await _add_chunk(db, nb_a, QUOTE_OWN)
    sid_foreign, cid_foreign = await _add_chunk(db, nb_b, QUOTE_FOREIGN)
    _, cid_foreign_2 = await _add_chunk(db, nb_b, QUOTE_FOREIGN)
    _stub_retrieval(
        monkeypatch,
        [
            (sid_own, cid_own, QUOTE_OWN),
            (sid_foreign, cid_foreign, QUOTE_FOREIGN),
            (sid_own, cid_foreign_2, QUOTE_FOREIGN),
        ],
    )
    _stub_llm(
        monkeypatch,
        "에스프레소는 9기압의 압력으로 25~30초간 추출합니다 [1]. "
        "다른 노트북의 기밀 발췌 문장이다 [2][3].",
    )

    done, assistant = await _ask(client, token_a, session_id)

    assert [(c["n"], c["source_id"], c["chunk_id"]) for c in done["citations"]] == [
        (1, sid_own, cid_own)
    ]
    assert assistant["citations"] == done["citations"]


async def test_autonomous_citations_match_chunks_by_chunk_row_id(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    """#190 회귀(대조 기준): 인용 chunk_id는 청크 행 id로 대조한다 — 같은 노트북 소스라도 어느
    행도 아닌 chunk_id를 단 인용은 버린다."""
    token = await login(client, user_a.email)
    nb_id, session_id = await _notebook_with_session(client, token)
    sid1, cid1 = await _add_chunk(db, nb_id, QUOTE_OWN)
    sid2, _ = await _add_chunk(db, nb_id, QUOTE_OWN_2)
    stray = str(uuid.uuid4())
    _stub_retrieval(monkeypatch, [(sid1, cid1, QUOTE_OWN), (sid2, stray, QUOTE_OWN_2)])
    _stub_llm(monkeypatch, f"{QUOTE_OWN} [1]. {QUOTE_OWN_2} [2].")

    done, assistant = await _ask(client, token, session_id)

    assert [(c["n"], c["chunk_id"]) for c in done["citations"]] == [(1, cid1)]
    assert assistant["citations"] == done["citations"]


async def test_autonomous_without_citation_markers_falls_back_like_static(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    """AC-3: 마커가 없으면 정적 경로(filter_cited)와 같이 검색한 근거 전체를 노출한다."""
    token = await login(client, user_a.email)
    nb_id, session_id = await _notebook_with_session(client, token)
    sid1, cid1 = await _add_chunk(db, nb_id, QUOTE_OWN)
    sid2, cid2 = await _add_chunk(db, nb_id, QUOTE_OWN_2)
    _stub_retrieval(monkeypatch, [(sid1, cid1, QUOTE_OWN), (sid2, cid2, QUOTE_OWN_2)])
    _stub_llm(monkeypatch, "커피 추출 방식은 여러 가지입니다.")

    done, assistant = await _ask(client, token, session_id)

    assert [c["chunk_id"] for c in done["citations"]] == [cid1, cid2]
    assert assistant["citations"] == done["citations"]


async def test_autonomous_react_system_prompt_requires_citations(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    """AC-4: react 노드 system 프롬프트가 정적 rag_chat 경로와 같은 [n] 인용 규칙을 담는다."""
    token = await login(client, user_a.email)
    _, session_id = await _notebook_with_session(client, token)
    _stub_retrieval(monkeypatch, [])
    seen = _stub_llm(monkeypatch, "답변")

    await _ask(client, token, session_id)

    system_text = "\n".join(m["content"] for m in seen[0] if m["role"] == "system")
    assert AUTONOMOUS_CITATION_RULE in system_text
    # source_load(전체 원문)는 코어가 인용 번호를 매기지 않는다 — 라이브에서 근거 없는 [1]이
    # 남았다. 번호는 rag_search 발췌에만 단다는 범위 규칙이 함께 실려야 한다.
    assert AUTONOMOUS_CITATION_SCOPE in system_text
    assert "source_load" in AUTONOMOUS_CITATION_SCOPE


def test_autonomous_citation_rule_matches_static_rag_chat_wording():
    """AC-4: 문구는 코어 정적 경로 규칙 2번을 그대로 쓴다(공백 정규화 후 부분 문자열)."""

    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    assert norm(AUTONOMOUS_CITATION_RULE) in norm(rag_chat.SYSTEM_PROMPT)


async def test_autonomous_citation_failure_still_saves_answer(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    """AC-5: 인용 검증이 실패해도 답변은 저장되고 citations는 빈 목록이다(답변 우선)."""
    token = await login(client, user_a.email)
    nb_id, session_id = await _notebook_with_session(client, token)
    sid1, cid1 = await _add_chunk(db, nb_id, QUOTE_OWN)
    _stub_retrieval(monkeypatch, [(sid1, cid1, QUOTE_OWN)])
    _stub_llm(monkeypatch, "에스프레소는 9기압의 압력으로 25~30초간 추출합니다 [1].")

    async def broken_verify(db_, citations, answer):
        raise RuntimeError("embedder down")

    monkeypatch.setattr("app.api.v1.chat.verify_citations", broken_verify)

    done, assistant = await _ask(client, token, session_id)

    assert done["citations"] == []
    assert assistant["citations"] == []
    assert "[1]" in assistant["content"]


async def test_autonomous_citation_db_error_still_saves_answer(
    client: httpx.AsyncClient, db, user_a, llm_seed, monkeypatch
):
    """AC-5(DB 오류): 인용 단계의 실제 DB 오류(asyncpg UndefinedTable)가 세션 트랜잭션을
    망가뜨려도 답변은 저장되고 citations는 빈 목록이다 — 인용 단계만 SAVEPOINT로 되돌린다."""
    from sqlalchemy import text

    import app.services.provenance as provenance_mod

    token = await login(client, user_a.email)
    nb_id, session_id = await _notebook_with_session(client, token)
    sid1, cid1 = await _add_chunk(db, nb_id, QUOTE_OWN)
    _stub_retrieval(monkeypatch, [(sid1, cid1, QUOTE_OWN)])
    _stub_llm(monkeypatch, "에스프레소는 9기압의 압력으로 25~30초간 추출합니다 [1].")

    real_filter = provenance_mod.filter_citations_to_notebook

    async def filter_then_db_error(db_, notebook_id, citations):
        await real_filter(db_, notebook_id, citations)
        await db_.execute(text("SELECT 1 FROM no_such_table_161"))
        raise AssertionError("unreachable")

    monkeypatch.setattr("app.api.v1.chat.filter_citations_to_notebook", filter_then_db_error)

    done, assistant = await _ask(client, token, session_id)

    assert done["citations"] == []
    assert assistant["citations"] == []
    assert "[1]" in assistant["content"]
    assert assistant["provenance"]["mode"] == "autonomous"
