"""메모(Note) API 테스트 — CRUD, 권한, 소스 변환."""

import uuid

import httpx
import pytest

from tests.conftest import bearer, login, set_source_limit, share_group


@pytest.fixture(autouse=True)
def _mock_external(monkeypatch):
    """소스 변환 경로의 스토리지/Celery 의존성 차단."""
    stored: dict[tuple[str, str], bytes] = {}

    def fake_put(bucket, key, data, content_type="application/octet-stream"):
        stored[(bucket, key)] = data

    class FakeAsyncResult:
        id = "fake-task-id"

    monkeypatch.setattr("app.api.v1.notes.ensure_buckets", lambda: None)
    monkeypatch.setattr("app.api.v1.notes.put_object", fake_put)
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.delay", lambda sid: FakeAsyncResult()
    )
    monkeypatch.setattr(
        "biznotes_core.workers.tasks.ingestion.ingest_source.apply_async",
        lambda *a, **k: FakeAsyncResult(),
    )
    return stored


async def _login_token(client, email: str) -> str:
    """로그인 후 쿠키 제거 — 쿠키가 Bearer보다 우선이므로 다중 사용자 테스트에서는 헤더만 사용."""
    token = await login(client, email)
    client.cookies.clear()
    return token


async def _make_notebook(client, token) -> str:
    res = await client.post("/api/v1/notebooks", json={"title": "note-nb"}, headers=bearer(token))
    return res.json()["id"]


async def _post_note(client, token, nb_id, **overrides) -> httpx.Response:
    body = {"title": "테스트 메모", "content": "메모 본문입니다.", **overrides}
    return await client.post(f"/api/v1/notebooks/{nb_id}/notes", json=body, headers=bearer(token))


async def _make_note(client, token, nb_id, **overrides) -> dict:
    res = await _post_note(client, token, nb_id, **overrides)
    assert res.status_code == 201, res.text
    return res.json()


async def _make_chat_session(client, token, nb_id: str) -> str:
    res = await client.post(f"/api/v1/notebooks/{nb_id}/chat/sessions", headers=bearer(token))
    assert res.status_code in (200, 201), res.text
    return res.json()["id"]


async def _add_message(db, session_id: str, **overrides):
    from biznotes_core.models import ChatMessage

    message = ChatMessage(
        session_id=uuid.UUID(session_id),
        role=overrides.pop("role", "assistant"),
        content=overrides.pop("content", "assistant 답변"),
        citations=overrides.pop("citations", []),
        token_usage={},
        provenance=overrides.pop("provenance", None),
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


async def _make_agent_run(db, notebook_id: str, user_id, **overrides):
    """AgentRun(+소유 Agent) 직접 삽입 — Celery/워크플로우 실행 없이 스냅샷 결합만 검증."""
    from biznotes_core.models import Agent, AgentRun

    agent = Agent(
        name=overrides.pop("agent_name", "테스트 에이전트"), kind=overrides.pop("kind", "workflow")
    )
    db.add(agent)
    await db.flush()
    run = AgentRun(
        agent_id=agent.id,
        notebook_id=uuid.UUID(notebook_id) if isinstance(notebook_id, str) else notebook_id,
        user_id=user_id,
        status=overrides.pop("status", "succeeded"),
        artifact_keys=overrides.pop("artifact_keys", []),
        skill_version_id=overrides.pop("skill_version_id", None),
        skill_package_hash=overrides.pop("skill_package_hash", None),
        skill_snapshot=overrides.pop("skill_snapshot", None),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run, agent


@pytest.fixture
def _mock_artifact_store(monkeypatch):
    """`services/storage.get_object`/`stat_object_size` 모킹 — origin=agent 결합이
    읽는 스토리지 원문(및 상한 검사 전 크기 조회, B-05)."""
    store: dict[str, bytes] = {}

    def fake_get_object(bucket: str, key: str) -> bytes:
        return store[key]

    def fake_stat_object_size(bucket: str, key: str) -> int:
        return len(store[key])

    monkeypatch.setattr("biznotes_core.services.storage.get_object", fake_get_object)
    monkeypatch.setattr("biznotes_core.services.storage.stat_object_size", fake_stat_object_size)
    return store


async def test_note_crud(client: httpx.AsyncClient, user_a):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    note = await _make_note(client, token, nb_id)
    assert note["origin"] == "manual"
    assert note["author_id"]

    # 목록
    res = await client.get(f"/api/v1/notebooks/{nb_id}/notes", headers=bearer(token))
    assert res.status_code == 200
    assert len(res.json()) == 1

    # 수정
    res = await client.patch(
        f"/api/v1/notes/{note['id']}",
        json={"title": "수정된 제목", "content": "수정된 본문"},
        headers=bearer(token),
    )
    assert res.status_code == 200
    assert res.json()["title"] == "수정된 제목"

    # 삭제
    res = await client.delete(f"/api/v1/notes/{note['id']}", headers=bearer(token))
    assert res.status_code == 204
    res = await client.get(f"/api/v1/notebooks/{nb_id}/notes", headers=bearer(token))
    assert res.json() == []


async def _add_source_with_chunk(db, notebook_id: str) -> tuple[str, str]:
    """소스 1개와 청크 1개를 만들고 `(source_id, 인용 chunk_id)`를 돌려준다.

    인용의 `chunk_id`는 청크 행 id(`SourceChunk.id`)다 — 검색(pgvector) 결과의 id가 곧 행 id다.
    """
    from biznotes_core.models import Source, SourceChunk

    source = Source(notebook_id=uuid.UUID(notebook_id), type="text", title="소스", status="ready")
    db.add(source)
    await db.flush()
    chunk = SourceChunk(source_id=source.id, seq=0, text="청크 본문")
    db.add(chunk)
    await db.commit()
    return str(source.id), str(chunk.id)


async def test_note_from_chat_with_citations(client: httpx.AsyncClient, user_a, db):
    """AC-7/AC-8: origin=chat은 실제 assistant 메시지에서 본문·인용·provenance를
    서버가 결합한다. 클라이언트가 보낸 content/citations는 무시된다."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    source_id, chunk_id = await _add_source_with_chunk(db, nb_id)
    session_id = await _make_chat_session(client, token, nb_id)

    citations = [
        {
            "n": 1,
            "source_id": source_id,
            "chunk_id": chunk_id,
            "quote": "인용문",
            "source_title": "소스",
        }
    ]
    message = await _add_message(
        db,
        session_id,
        content="assistant 답변 내용",
        citations=citations,
        provenance={
            "v": 1,
            "mode": "static",
            "recorded_at": "2026-09-02T09:00:00Z",
            "run_id": None,
            "artifact_name": None,
            "content_sha256": None,
            "items": [
                {"kind": "source", "source_id": source_id, "title": "소스", "status": "succeeded"}
            ],
        },
    )

    # 클라이언트가 보내는 content/citations는 origin=chat에서 무시된다(AC-8f).
    note = await _make_note(
        client,
        token,
        nb_id,
        origin="chat",
        origin_ref=str(message.id),
        content="클라이언트가 보낸 위조 본문",
        citations=[],
    )
    assert note["origin"] == "chat"
    assert note["content"] == "assistant 답변 내용"
    # R4-3b: NoteOut.citations를 list[CitationOut]로 좁히면서 `verified`가 응답에
    # 명시된다(기본 True). 프론트는 `verified === false`만 미확인으로 렌더하므로
    # 화면 동작은 불변이다(citation-text.tsx:14).
    assert note["citations"] == [{**citations[0], "verified": True}]
    assert note["provenance"]["mode"] == "static"
    assert note["provenance"]["items"][0]["kind"] == "source"
    assert note["content_intact"] is True


async def test_note_from_legacy_message_gets_unrecorded_provenance(
    client: httpx.AsyncClient, user_a, db
):
    """AC-18: provenance NULL(레거시/기록 실패) assistant 메시지에서도 노트가 만들어지되,
    mode="unrecorded" 최소 스냅샷이 붙는다(D14). GET/list/PATCH 후 모두 동일하게 보인다."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    session_id = await _make_chat_session(client, token, nb_id)
    message = await _add_message(db, session_id, content="과거 답변", provenance=None)

    note = await _make_note(client, token, nb_id, origin="chat", origin_ref=str(message.id))
    assert note["content"] == "과거 답변"
    assert note["citations"] == []
    assert note["provenance"]["mode"] == "unrecorded"
    assert note["provenance"]["items"] == []
    assert note["content_intact"] is True

    res = await client.get(f"/api/v1/notes/{note['id']}", headers=bearer(token))
    assert res.json()["provenance"]["mode"] == "unrecorded"

    res = await client.get(f"/api/v1/notebooks/{nb_id}/notes", headers=bearer(token))
    assert res.json()[0]["provenance"]["mode"] == "unrecorded"

    res = await client.patch(
        f"/api/v1/notes/{note['id']}", json={"content": "수정됨"}, headers=bearer(token)
    )
    assert res.status_code == 200
    assert res.json()["content_intact"] is False
    assert res.json()["provenance"]["mode"] == "unrecorded"


async def test_chat_note_citations_refiltered_from_message(client: httpx.AsyncClient, user_a, db):
    """AC-8(g)(h): 메시지에 남아있던 citation이라도 노트북 소속이 아니면 노트 생성 시
    다시 걸러진다(메시지가 다른 노트북에서 이관/재사용될 가능성에 대비한 방어)."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    other_nb_id = await _make_notebook(client, token)
    other_source_id, other_chunk_id = await _add_source_with_chunk(db, other_nb_id)
    session_id = await _make_chat_session(client, token, nb_id)

    citations = [
        {
            "n": 1,
            "source_id": other_source_id,
            "chunk_id": other_chunk_id,
            "quote": "다른 노트북 인용",
            "source_title": "타 소스",
        }
    ]
    message = await _add_message(db, session_id, citations=citations)

    note = await _make_note(client, token, nb_id, origin="chat", origin_ref=str(message.id))
    assert note["citations"] == []


async def test_manual_note_content_intact_is_null(client: httpx.AsyncClient, user_a):
    """AC-10: manual 노트는 provenance가 없으므로 content_intact도 null이다."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    note = await _make_note(client, token, nb_id)
    assert note["provenance"] is None
    assert note["content_intact"] is None


async def test_content_intact_transitions(client: httpx.AsyncClient, user_a, db):
    """AC-10: 기록된 provenance가 있는 노트는 생성 직후 true, 편집 후 false로
    전이하고 provenance 자체(mode 등)는 불변이다."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    session_id = await _make_chat_session(client, token, nb_id)
    message = await _add_message(
        db,
        session_id,
        content="원본 답변",
        provenance={
            "v": 1,
            "mode": "autonomous",
            "recorded_at": "2026-09-02T09:00:00Z",
            "run_id": None,
            "artifact_name": None,
            "content_sha256": None,
            "items": [{"kind": "tool", "tool": "rag_search", "status": "succeeded"}],
        },
    )
    note = await _make_note(client, token, nb_id, origin="chat", origin_ref=str(message.id))
    assert note["content_intact"] is True

    res = await client.patch(
        f"/api/v1/notes/{note['id']}", json={"content": "편집된 답변"}, headers=bearer(token)
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content_intact"] is False
    assert body["provenance"]["mode"] == "autonomous"
    assert body["provenance"]["items"][0]["tool"] == "rag_search"


async def test_client_supplied_provenance_ignored(client: httpx.AsyncClient, user_a, db):
    """AC-8(f): 바디에 위조 `provenance`를 실어 보내도 서버가 서버 결합값으로 덮는다
    (`NoteCreate`에 애초에 필드가 없어 pydantic이 기본적으로 무시하지만, 위조 방지가
    이 phase의 핵심 보안 주장이므로 회귀 방지 단언을 남긴다)."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    session_id = await _make_chat_session(client, token, nb_id)
    message = await _add_message(db, session_id, content="원본 답변", provenance=None)

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/notes",
        json={
            "title": "위조 시도",
            "origin": "chat",
            "origin_ref": str(message.id),
            "provenance": {
                "v": 1,
                "mode": "agent_run",
                "recorded_at": "2020-01-01T00:00:00Z",
                "run_id": "위조-run-id",
                "artifact_name": "위조.md",
                "items": [
                    {"kind": "skill", "name": "위조 스킬", "version": 99, "status": "succeeded"}
                ],
            },
        },
        headers=bearer(token),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["provenance"]["mode"] == "unrecorded"  # 바디의 "agent_run" 위조가 무시됨
    assert body["provenance"]["items"] == []
    assert body["provenance"]["run_id"] is None
    assert body["provenance"]["artifact_name"] is None


# --- AC-7: origin_ref 참조 검증 부정 테스트 (모두 동일한 단일 422 detail, D7) -----------


async def _assert_note_rejected(client, token, nb_id, res: httpx.Response) -> None:
    """AC-7: 모든 거절 분기가 (a) 동일한 단일 422 detail(D7·B-02)이고
    (b) 노트가 실제로 만들어지지 않았는지(목록 공집합, B-03)까지 함께 단언한다."""
    assert res.status_code == 422, res.text
    assert res.json()["detail"] == "origin_ref가 유효하지 않습니다"
    listing = await client.get(f"/api/v1/notebooks/{nb_id}/notes", headers=bearer(token))
    assert listing.status_code == 200
    assert listing.json() == []


async def test_chat_note_rejects_missing_origin_ref(client: httpx.AsyncClient, user_a):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    res = await _post_note(client, token, nb_id, origin="chat")
    await _assert_note_rejected(client, token, nb_id, res)


async def test_chat_note_rejects_nonexistent_message(client: httpx.AsyncClient, user_a):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    res = await _post_note(client, token, nb_id, origin="chat", origin_ref=str(uuid.uuid4()))
    await _assert_note_rejected(client, token, nb_id, res)


async def test_chat_note_rejects_user_role_message(client: httpx.AsyncClient, user_a, db):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    session_id = await _make_chat_session(client, token, nb_id)
    message = await _add_message(db, session_id, role="user", content="사용자 질문")
    res = await _post_note(client, token, nb_id, origin="chat", origin_ref=str(message.id))
    await _assert_note_rejected(client, token, nb_id, res)


async def test_chat_note_rejects_other_users_session(client: httpx.AsyncClient, user_a, user_b, db):
    """같은(공유된) 노트북이라도 타인 소유 세션의 메시지는 origin_ref로 쓸 수 없다 —
    notebook_id 일치와 별개로 session.user_id 불일치만 단독으로 겨냥한다."""
    token_a = await _login_token(client, user_a.email)
    token_b = await _login_token(client, user_b.email)
    nb_id = await _make_notebook(client, token_a)
    group = await share_group(db, user_a, user_b)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/share",
        json={"group_id": str(group.id), "permission": "write"},
        headers=bearer(token_a),
    )
    assert res.status_code == 201, res.text
    b_session_id = await _make_chat_session(client, token_b, nb_id)
    message = await _add_message(db, b_session_id, content="b의 답변")

    res = await _post_note(client, token_a, nb_id, origin="chat", origin_ref=str(message.id))
    await _assert_note_rejected(client, token_a, nb_id, res)


async def test_chat_note_rejects_other_notebooks_session(client: httpx.AsyncClient, user_a, db):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    other_nb_id = await _make_notebook(client, token)
    other_session_id = await _make_chat_session(client, token, other_nb_id)
    message = await _add_message(db, other_session_id, content="다른 노트북 답변")

    res = await _post_note(client, token, nb_id, origin="chat", origin_ref=str(message.id))
    await _assert_note_rejected(client, token, nb_id, res)


async def test_chat_note_rejects_artifact_name_present(client: httpx.AsyncClient, user_a, db):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    session_id = await _make_chat_session(client, token, nb_id)
    message = await _add_message(db, session_id)
    res = await _post_note(
        client,
        token,
        nb_id,
        origin="chat",
        origin_ref=str(message.id),
        artifact_name="report.md",
    )
    await _assert_note_rejected(client, token, nb_id, res)


async def test_manual_note_rejects_origin_ref_present(client: httpx.AsyncClient, user_a):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    res = await _post_note(client, token, nb_id, origin_ref=str(uuid.uuid4()))
    await _assert_note_rejected(client, token, nb_id, res)


async def test_manual_note_rejects_artifact_name_present(client: httpx.AsyncClient, user_a):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    res = await _post_note(client, token, nb_id, artifact_name="report.md")
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_missing_artifact_name(client: httpx.AsyncClient, user_a, db):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    res = await _post_note(client, token, nb_id, origin="agent", origin_ref=str(run.id))
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_nonexistent_run(client: httpx.AsyncClient, user_a):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    res = await _post_note(
        client,
        token,
        nb_id,
        origin="agent",
        origin_ref=str(uuid.uuid4()),
        artifact_name="report.md",
    )
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_other_notebook_run(client: httpx.AsyncClient, user_a, db):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    other_nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        other_nb_id,
        user_a.id,
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    res = await _post_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="report.md"
    )
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_non_succeeded_run(client: httpx.AsyncClient, user_a, db):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        status="running",
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    res = await _post_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="report.md"
    )
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_unknown_artifact_name(client: httpx.AsyncClient, user_a, db):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    res = await _post_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="missing.md"
    )
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_non_text_content_type(
    client: httpx.AsyncClient, user_a, db, _mock_artifact_store
):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "audio.wav", "key": "k", "content_type": "audio/wav"}],
    )
    _mock_artifact_store["k"] = b"\x00\x01"
    res = await _post_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="audio.wav"
    )
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_oversized_content(
    client: httpx.AsyncClient, user_a, db, _mock_artifact_store
):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    _mock_artifact_store["k"] = ("가" * 200_001).encode("utf-8")
    res = await _post_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="report.md"
    )
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_oversized_content_without_downloading_body(
    client: httpx.AsyncClient, user_a, db, _mock_artifact_store, monkeypatch
):
    """B-05: 바이트 사전 게이트(`stat_object_size` > 800,000바이트)가 실제로 걸리고,
    그 경우 `get_object`(본문 전체 다운로드)는 호출조차 되지 않아야 한다 — 상한 검사
    "전에" 메모리로 읽지 않는다는 보장 자체를 회귀 방지한다(round2 C-01)."""
    get_object_calls: list[str] = []

    def tracking_get_object(bucket: str, key: str) -> bytes:
        get_object_calls.append(key)
        return _mock_artifact_store[key]

    monkeypatch.setattr("biznotes_core.services.storage.get_object", tracking_get_object)

    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    _mock_artifact_store["k"] = b"x" * 900_000  # > _MAX_AGENT_NOTE_CHARS(200_000) * 4
    res = await _post_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="report.md"
    )
    await _assert_note_rejected(client, token, nb_id, res)
    assert get_object_calls == []  # 본문을 전혀 내려받지 않았다


async def test_agent_note_rejects_malformed_json(
    client: httpx.AsyncClient, user_a, db, _mock_artifact_store
):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "data.json", "key": "k", "content_type": "application/json"}],
    )
    _mock_artifact_store["k"] = b"{not valid json"
    res = await _post_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="data.json"
    )
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_non_utf8_content(
    client: httpx.AsyncClient, user_a, db, _mock_artifact_store
):
    """text/* 게이트를 통과해도 UTF-8로 디코딩되지 않으면 거절된다."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    _mock_artifact_store["k"] = b"\xff\xfe\x00invalid-utf8"
    res = await _post_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="report.md"
    )
    await _assert_note_rejected(client, token, nb_id, res)


async def test_agent_note_rejects_storage_failure(
    client: httpx.AsyncClient, user_a, db, monkeypatch
):
    """스토리지 조회 자체가 실패해도(오브젝트 소실 등) 동일한 단일 detail로 거절된다."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )

    def fake_stat_object_size(bucket: str, key: str) -> int:
        raise RuntimeError("storage unreachable")

    monkeypatch.setattr("biznotes_core.services.storage.stat_object_size", fake_stat_object_size)
    res = await _post_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="report.md"
    )
    await _assert_note_rejected(client, token, nb_id, res)


# --- AC-9: origin=agent 스냅샷(typed extractor) ---------------------------------------


async def test_agent_note_snapshot_end_to_end(
    client: httpx.AsyncClient, user_a, db, _mock_artifact_store
):
    """스킬 버전·MCP 서버명 조인·legacy(=call_status 부재) 로그의 unknown 판정까지
    한 번에 검증한다."""
    from datetime import UTC, datetime

    from biznotes_core.models import ExecutionLog, MCPServer, Skill, SkillVersion

    skill = Skill(
        name="건축물 용도변경 검토 보고서",
        description="d",
        slug=f"snapshot-{uuid.uuid4().hex[:6]}",
        visibility="system",
        status="published",
    )
    db.add(skill)
    await db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version=3,
        package_uri="skills/snapshot/3",
        skill_md_hash="a" * 64,
        package_hash="b" * 64,
        manifest={},
        file_tree=[],
        security_report={},
        status="published",
    )
    db.add(version)
    server = MCPServer(
        name="archhub", transport="streamable_http", endpoint_or_command="https://x", enabled=True
    )
    db.add(server)
    await db.flush()

    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        agent_name="용도변경 검토 wrapper",
        skill_version_id=version.id,
        skill_package_hash=version.package_hash,
        skill_snapshot={"name": skill.name, "package_hash": version.package_hash},
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    server_tag = str(server.id).replace("-", "")[:8]
    now = datetime.now(UTC)
    db.add_all(
        [
            ExecutionLog(
                run_id=run.id,
                node_id="react:tool:0",
                node_type="react_tool_call",
                status="succeeded",
                input_data={
                    "tool": f"mcp_{server_tag}_find_region",
                    "arguments": {},
                    "call_status": "succeeded",
                },
                started_at=now,
                finished_at=now,
            ),
            ExecutionLog(
                run_id=run.id,
                node_id="react:tool:1",
                node_type="react_tool_call",
                status="succeeded",  # 과거(Phase 1 이전) 행 — call_status 자체가 없다.
                input_data={"tool": "rag_search", "arguments": {}},
                started_at=now,
                finished_at=now,
            ),
        ]
    )
    await db.commit()
    _mock_artifact_store["k"] = "# 보고서\n본문".encode()

    note = await _make_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="report.md"
    )
    assert note["origin"] == "agent"
    assert note["content"] == "# 보고서\n본문"
    assert note["citations"] == []
    prov = note["provenance"]
    assert prov["mode"] == "agent_run"
    assert prov["run_id"] == str(run.id)
    assert prov["artifact_name"] == "report.md"
    kinds = {item["kind"]: item for item in prov["items"]}
    assert kinds["agent"]["name"] == "용도변경 검토 wrapper"
    assert kinds["agent"]["status"] == "succeeded"
    assert kinds["skill"]["name"] == skill.name
    assert kinds["skill"]["version"] == 3
    assert "skill_version_id" not in kinds["skill"]  # 내부 필드 비공개(R-05)
    assert kinds["mcp"]["server"] == "archhub"
    assert kinds["mcp"]["status"] == "succeeded"
    tool_item = next(i for i in prov["items"] if i.get("tool") == "rag_search")
    assert tool_item["status"] == "unknown"  # call_status 부재 → unknown(R2-02)
    assert note["content_intact"] is True


async def test_agent_note_snapshot_graph_mcp_node_joins_server_name(
    client: httpx.AsyncClient, user_a, db, _mock_artifact_store
):
    """B-01: kind="mcp" 에이전트(graph.py::kind_to_nodes — mcp_tool → save_artifact
    두 노드로만 구성)의 run은 `node_type="mcp_tool"` ExecutionLog만 남긴다. 이 로그의
    input_data는 react 경로와 달리 `{server_id, tool_name}`이고 노출명이 없으며,
    상태는 `input_data.call_status`가 아니라 `ExecutionLog.status` 행 컬럼에서 온다."""
    from biznotes_core.models import ExecutionLog, MCPServer

    server = MCPServer(
        name="archhub", transport="streamable_http", endpoint_or_command="https://x", enabled=True
    )
    db.add(server)
    await db.flush()

    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        agent_name="archhub 조회 에이전트",
        kind="mcp",
        artifact_keys=[{"name": "mcp-result.md", "key": "k", "content_type": "text/markdown"}],
    )
    db.add(
        ExecutionLog(
            run_id=run.id,
            node_id="call",
            node_type="mcp_tool",
            status="succeeded",
            input_data={"server_id": str(server.id), "tool_name": "find_region", "arguments": {}},
        )
    )
    await db.commit()
    _mock_artifact_store["k"] = "경기도 부천시".encode()

    note = await _make_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="mcp-result.md"
    )
    prov = note["provenance"]
    mcp_items = [i for i in prov["items"] if i["kind"] == "mcp"]
    assert len(mcp_items) == 1
    assert mcp_items[0]["server"] == "archhub"
    assert mcp_items[0]["tool"] == "find_region"
    assert mcp_items[0]["status"] == "succeeded"


async def test_agent_note_snapshot_orders_items_by_started_at_not_insertion(
    client: httpx.AsyncClient, user_a, db, _mock_artifact_store
):
    """B-06: `ExecutionLog.id`는 random UUID4라 삽입 순서를 반영하지 않는다(models/
    base.py::UUIDPrimaryKeyMixin) — 같은 kind 내부 순서는 `started_at`으로만 보존돼야
    한다. 늦게 호출된(=started_at이 나중인) 도구를 먼저 삽입해 정렬을 검증한다."""
    from datetime import UTC, datetime, timedelta

    from biznotes_core.models import ExecutionLog

    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, _agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    base = datetime.now(UTC)
    # 삽입 순서를 실제 호출 순서와 반대로 만든다: earlier(호출 순서상 첫 번째)를
    # 두 번째로 커밋한다.
    later = ExecutionLog(
        run_id=run.id,
        node_id="react:tool:1",
        node_type="react_tool_call",
        status="succeeded",
        input_data={"tool": "source_load", "arguments": {}, "call_status": "succeeded"},
        started_at=base + timedelta(seconds=5),
        finished_at=base + timedelta(seconds=6),
    )
    db.add(later)
    earlier = ExecutionLog(
        run_id=run.id,
        node_id="react:tool:0",
        node_type="react_tool_call",
        status="succeeded",
        input_data={"tool": "rag_search", "arguments": {}, "call_status": "succeeded"},
        started_at=base,
        finished_at=base + timedelta(seconds=1),
    )
    db.add(earlier)
    await db.commit()
    _mock_artifact_store["k"] = b"content"

    note = await _make_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="report.md"
    )
    tool_items = [i for i in note["provenance"]["items"] if i["kind"] == "tool"]
    assert [i["tool"] for i in tool_items] == ["rag_search", "source_load"]


# --- AC-11: CASCADE 후에도 노트 본문·provenance는 보존된다(스냅샷 복제, D3) ---------------


async def test_chat_note_survives_session_cascade(client: httpx.AsyncClient, user_a, db):
    from biznotes_core.models import ChatSession
    from sqlalchemy import delete

    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    session_id = await _make_chat_session(client, token, nb_id)
    message = await _add_message(db, session_id, content="세션 삭제 전 답변")
    note = await _make_note(client, token, nb_id, origin="chat", origin_ref=str(message.id))

    await db.execute(delete(ChatSession).where(ChatSession.id == uuid.UUID(session_id)))
    await db.commit()

    res = await client.get(f"/api/v1/notes/{note['id']}", headers=bearer(token))
    assert res.status_code == 200
    body = res.json()
    assert body["content"] == "세션 삭제 전 답변"
    assert body["provenance"] is not None


async def test_agent_note_survives_agent_cascade(
    client: httpx.AsyncClient, user_a, db, _mock_artifact_store
):
    from biznotes_core.models import Agent
    from sqlalchemy import delete

    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    run, agent = await _make_agent_run(
        db,
        nb_id,
        user_a.id,
        artifact_keys=[{"name": "report.md", "key": "k", "content_type": "text/markdown"}],
    )
    _mock_artifact_store["k"] = b"# report"
    note = await _make_note(
        client, token, nb_id, origin="agent", origin_ref=str(run.id), artifact_name="report.md"
    )

    await db.execute(delete(Agent).where(Agent.id == agent.id))
    await db.commit()

    res = await client.get(f"/api/v1/notes/{note['id']}", headers=bearer(token))
    assert res.status_code == 200
    body = res.json()
    assert body["content"] == "# report"
    assert body["provenance"] is not None
    assert body["provenance"]["run_id"] == str(run.id)


async def test_note_citation_from_other_notebook_is_filtered(client: httpx.AsyncClient, user_a, db):
    """다른 노트북 소속 source_id/chunk_id citation은 저장에서 제외된다(F14, IDOR 방지)."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    other_nb_id = await _make_notebook(client, token)
    other_source_id, other_chunk_id = await _add_source_with_chunk(db, other_nb_id)

    citations = [
        {
            "n": 1,
            "source_id": other_source_id,
            "chunk_id": other_chunk_id,
            "quote": "다른 노트북 인용",
            "source_title": "타 소스",
        }
    ]
    # origin은 기본값(manual) — citation IDOR 필터는 origin 결합 로직과 무관한
    # 관심사이므로 R2-12에서 분리했다(전에는 여기서도 origin="chat"을 사용했으나
    # Phase 2부터 origin="chat"은 실제 assistant 메시지 origin_ref를 요구한다).
    note = await _make_note(client, token, nb_id, citations=citations)
    assert note["citations"] == []


async def test_note_citation_with_nonexistent_ids_is_filtered(client: httpx.AsyncClient, user_a):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    citations = [
        {
            "n": 1,
            "source_id": "s1",  # 유효한 UUID가 아님
            "chunk_id": "c1",
            "quote": "인용문",
            "source_title": "소스",
        }
    ]
    note = await _make_note(client, token, nb_id, citations=citations)
    assert note["citations"] == []


async def test_note_citation_with_mismatched_source_and_chunk_is_filtered(
    client: httpx.AsyncClient, user_a, db
):
    """codex 리뷰 지적: 같은 노트북 내에서도 chunk_id가 주장된 source_id가 아니라
    다른 source에 속하면(뒤섞인 citation) 저장에서 제외되어야 한다."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    source_a_id, _ = await _add_source_with_chunk(db, nb_id)
    source_b_id, chunk_b_id = await _add_source_with_chunk(db, nb_id)

    citations = [
        {
            "n": 1,
            "source_id": source_a_id,  # source A라고 주장하지만
            "chunk_id": chunk_b_id,  # 실제로는 source B의 청크
            "quote": "뒤섞인 인용",
            "source_title": "소스",
        }
    ]
    note = await _make_note(client, token, nb_id, citations=citations)
    assert note["citations"] == []

    # source_id를 실제 소속(B)으로 바로잡으면 정상 보존된다
    citations[0]["source_id"] = source_b_id
    note2 = await _make_note(client, token, nb_id, citations=citations)
    # `verified`는 응답에서 기본 True로 명시된다(R4-3b) — 위 테스트 주석 참고.
    assert note2["citations"] == [{**citations[0], "verified": True}]


def _citation(n: int, source_id: str, chunk_id: str) -> dict:
    return {
        "n": n,
        "source_id": source_id,
        "chunk_id": chunk_id,
        "quote": "인용문",
        "source_title": "소스",
    }


async def test_note_citation_chunk_id_is_the_chunk_row_id(client: httpx.AsyncClient, user_a, db):
    """인용 chunk_id(청크 행 id)로 같은 노트북 청크를 찾으면 보존하고, 어느 행도 아닌 id는
    통과하지 않는다(#190의 대조 기준 — pgvector로 옮기며 point id가 행 id가 됐다)."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    source_id, chunk_id = await _add_source_with_chunk(db, nb_id)

    note = await _make_note(client, token, nb_id, citations=[_citation(1, source_id, chunk_id)])
    assert note["citations"] == [{**_citation(1, source_id, chunk_id), "verified": True}]

    stray = str(uuid.uuid4())
    note = await _make_note(client, token, nb_id, citations=[_citation(1, source_id, stray)])
    assert note["citations"] == []


async def test_chat_note_citation_with_unknown_chunk_is_filtered(
    client: httpx.AsyncClient, user_a, db
):
    """chat 노트도 원본 메시지 인용을 청크 행 id로 다시 거른다 — 어느 행도 아닌 id는 거부."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    source_id, chunk_id = await _add_source_with_chunk(db, nb_id)
    session_id = await _make_chat_session(client, token, nb_id)
    message = await _add_message(
        db,
        session_id,
        citations=[_citation(1, source_id, chunk_id), _citation(2, source_id, str(uuid.uuid4()))],
    )

    note = await _make_note(client, token, nb_id, origin="chat", origin_ref=str(message.id))
    assert [c["n"] for c in note["citations"]] == [1]


async def test_note_citation_unknown_chunk_dropped_others_kept(
    client: httpx.AsyncClient, user_a, db
):
    """#190: 존재하지 않거나 UUID가 아닌 chunk_id만 빠지고 정상 인용은 남는다."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    source_id, chunk_id = await _add_source_with_chunk(db, nb_id)

    citations = [
        _citation(1, source_id, chunk_id),
        _citation(2, source_id, str(uuid.uuid4())),
        _citation(3, source_id, "not-a-uuid"),
    ]
    note = await _make_note(client, token, nb_id, citations=citations)
    assert [c["n"] for c in note["citations"]] == [1]


async def test_note_citation_chunk_must_belong_to_the_cited_source(
    client: httpx.AsyncClient, user_a, db
):
    """청크 행 id는 유일하다 — 인용의 source_id가 그 청크의 소스가 아니면(뒤섞인 인용) 거부하고,
    다른 노트북 청크도 거부한다."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    other_nb_id = await _make_notebook(client, token)
    source_a_id, chunk_a = await _add_source_with_chunk(db, nb_id)
    source_b_id, _ = await _add_source_with_chunk(db, nb_id)
    other_source_id, other_chunk = await _add_source_with_chunk(db, other_nb_id)

    citations = [
        _citation(1, source_a_id, chunk_a),
        _citation(2, source_b_id, chunk_a),
        _citation(3, other_source_id, other_chunk),
    ]
    note = await _make_note(client, token, nb_id, citations=citations)
    assert [c["n"] for c in note["citations"]] == [1]


async def test_note_permissions(client: httpx.AsyncClient, db, user_a, user_b):
    """비공유 사용자는 404, 읽기 공유 사용자는 조회만 가능."""
    token_a = await _login_token(client, user_a.email)
    token_b = await _login_token(client, user_b.email)
    nb_id = await _make_notebook(client, token_a)
    note = await _make_note(client, token_a, nb_id)

    # 비공유 사용자 — 목록/수정 모두 404
    res = await client.get(f"/api/v1/notebooks/{nb_id}/notes", headers=bearer(token_b))
    assert res.status_code == 404
    res = await client.patch(
        f"/api/v1/notes/{note['id']}", json={"title": "x"}, headers=bearer(token_b)
    )
    assert res.status_code == 404

    # 읽기 공유 → 조회 가능, 생성/수정/삭제 불가
    group = await share_group(db, user_a, user_b)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/share",
        json={"group_id": str(group.id), "permission": "read"},
        headers=bearer(token_a),
    )
    assert res.status_code == 201
    res = await client.get(f"/api/v1/notebooks/{nb_id}/notes", headers=bearer(token_b))
    assert res.status_code == 200
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/notes",
        json={"title": "b의 메모", "content": "x"},
        headers=bearer(token_b),
    )
    assert res.status_code == 403
    res = await client.patch(
        f"/api/v1/notes/{note['id']}", json={"title": "x"}, headers=bearer(token_b)
    )
    assert res.status_code == 403
    res = await client.delete(f"/api/v1/notes/{note['id']}", headers=bearer(token_b))
    assert res.status_code == 403


async def test_note_write_share_can_create_but_not_edit_others(
    client: httpx.AsyncClient, db, user_a, user_b
):
    """쓰기 공유 사용자는 메모 생성 가능, 타인 메모 수정은 불가(작성자/소유자만)."""
    token_a = await _login_token(client, user_a.email)
    token_b = await _login_token(client, user_b.email)
    nb_id = await _make_notebook(client, token_a)
    note_a = await _make_note(client, token_a, nb_id)

    group = await share_group(db, user_a, user_b)
    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/share",
        json={"group_id": str(group.id), "permission": "write"},
        headers=bearer(token_a),
    )
    assert res.status_code == 201

    # b 생성 가능
    note_b = await _make_note(client, token_b, nb_id, title="b의 메모")
    # b는 a의 메모 수정 불가
    res = await client.patch(
        f"/api/v1/notes/{note_a['id']}", json={"title": "x"}, headers=bearer(token_b)
    )
    assert res.status_code == 403
    # 노트북 소유자 a는 b의 메모 수정 가능
    res = await client.patch(
        f"/api/v1/notes/{note_b['id']}", json={"title": "소유자 수정"}, headers=bearer(token_a)
    )
    assert res.status_code == 200


async def test_convert_note_to_source(client: httpx.AsyncClient, user_a, _mock_external):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    note = await _make_note(client, token, nb_id, title="변환할 메모", content="소스가 될 내용")

    res = await client.post(f"/api/v1/notes/{note['id']}/convert-to-source", headers=bearer(token))
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["type"] == "text"
    assert body["title"] == "변환할 메모"
    assert body["status"] == "pending"

    # 스토리지에 마크다운으로 저장됨
    stored_texts = [v.decode() for v in _mock_external.values()]
    assert any("# 변환할 메모" in t and "소스가 될 내용" in t for t in stored_texts)

    # 소스 목록에 반영
    res = await client.get(f"/api/v1/notebooks/{nb_id}/sources", headers=bearer(token))
    assert len(res.json()) == 1


async def test_convert_note_to_source_respects_notebook_source_limit(
    client: httpx.AsyncClient, user_a, _mock_external, monkeypatch
):
    """제품 경로(노트 → 소스)도 코어 상한 설정(#445)을 따른다 — #281 AC-6."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    note = await _make_note(client, token, nb_id, title="t", content="본문")
    set_source_limit(monkeypatch, 0)

    res = await client.post(f"/api/v1/notes/{note['id']}/convert-to-source", headers=bearer(token))
    assert res.status_code == 422, res.text


async def test_convert_empty_note_rejected(client: httpx.AsyncClient, user_a):
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)
    note = await _make_note(client, token, nb_id, content="   ")

    res = await client.post(f"/api/v1/notes/{note['id']}/convert-to-source", headers=bearer(token))
    assert res.status_code == 422


# --- R4CR-01 (blocking, codex 교차 리뷰) ---------------------------------------
#
# 응답(`NoteOut.citations`)만 `list[CitationOut]`으로 좁히고 요청(`NoteCreate`)을
# `list[dict[str, Any]]`로 열어두면, `_filter_citations_to_notebook()`이 source_id·chunk_id
# 소속만 검사하기 때문에 **둘 다 없는** citation이 두 가드를 모두 건너뛰고 저장된다.
# 그 뒤 응답 직렬화가 필수 5필드를 찾지 못해 500이 나고, 행이 남아 그 노트북의
# `GET /notes`가 영구적으로 500이 된다(poison row). 요청도 같은 형태로 검증해 막는다.


async def test_malformed_citation_is_rejected_not_persisted(
    client: httpx.AsyncClient, user_a, db
) -> None:
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/notes",
        json={"title": "malformed", "content": "x", "origin": "chat", "citations": [{}]},
        headers=bearer(token),
    )
    # 500(저장 후 직렬화 실패)이 아니라 422(요청 거절)여야 한다.
    assert res.status_code == 422, res.text

    # 아무것도 저장되지 않았고, 목록 조회가 여전히 살아 있어야 한다(poison row 부재).
    listing = await client.get(f"/api/v1/notebooks/{nb_id}/notes", headers=bearer(token))
    assert listing.status_code == 200
    assert listing.json() == []


async def test_citation_missing_required_field_is_rejected(
    client: httpx.AsyncClient, user_a
) -> None:
    """source_id/chunk_id는 있으나 quote 등 나머지 필수 필드가 빠진 경우도 422."""
    token = await _login_token(client, user_a.email)
    nb_id = await _make_notebook(client, token)

    res = await client.post(
        f"/api/v1/notebooks/{nb_id}/notes",
        json={
            "title": "partial",
            "content": "x",
            "origin": "chat",
            "citations": [{"n": 1, "source_id": str(uuid.uuid4()), "chunk_id": str(uuid.uuid4())}],
        },
        headers=bearer(token),
    )
    assert res.status_code == 422, res.text
