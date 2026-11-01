"""MCP 도구 승인 CLI(`scripts/mcp_approvals.py`) — AC-2.6 · HCI-4 (a) · 계약 CT-MCP-APPROVALS.

CLI의 `run(argv)`를 그대로 부른다(대체 없음). 쓰기는 코어 `set_approved_tools`를 거쳐 이름 규칙·
감사(`mcp.tools_approve`)를 상속한다. conftest 격리 DB.
"""

import pytest
from biznotes_core.core.db import async_session_factory
from biznotes_core.models import AuditLog, MCPServer, MCPServerTool
from sqlalchemy import select

from scripts.mcp_approvals import run


@pytest.fixture
async def server(db):
    row = MCPServer(name="demo", transport="stdio", endpoint_or_command="/bin/true")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _approvals() -> list[tuple[str, bool]]:
    async with async_session_factory() as s:
        rows = (
            await s.execute(select(MCPServerTool).order_by(MCPServerTool.tool_name))
        ).scalars()
        return [(r.tool_name, r.read_only) for r in rows]


async def test_set_and_show(server, admin_user, capsys):
    """set: 전체 교체(접미사 없으면 :ro) + 감사 actor, show: 서버별 목록."""
    actor = ["--actor-email", admin_user.email]
    rc = await run(["set", "--server", "demo", *actor, "word_count", "echo_upper:rw"])
    assert rc == 0
    assert "✅ demo: 승인 2개로 교체" in capsys.readouterr().out
    assert await _approvals() == [("echo_upper", False), ("word_count", True)]

    # 전체 교체 — 나열하지 않은 승인은 지운다
    assert await run(
        ["set", "--server", "demo", "--actor-email", admin_user.email, "reverse_text:ro"]
    ) == 0
    assert await _approvals() == [("reverse_text", True)]

    capsys.readouterr()
    assert await run(["show", "--server", "demo"]) == 0
    out = capsys.readouterr().out
    assert f"demo\treverse_text\ttrue\t{admin_user.email}\t" in out
    assert "승인 1건" in out
    assert await run(["show"]) == 0

    async with async_session_factory() as s:
        audits = (
            await s.execute(select(AuditLog).where(AuditLog.action == "mcp.tools_approve"))
        ).scalars().all()
    assert len(audits) == 2
    assert {a.user_id for a in audits} == {admin_user.id}
    assert {a.resource for a in audits} == {"demo"}


@pytest.mark.parametrize(
    "specs",
    [["bad name"], ["a/b"], ["x" * 129], ["word_count:maybe"], ["word_count:ro", "word_count:rw"]],
    ids=["space", "slash", "too-long", "bad-suffix", "duplicate"],
)
async def test_invalid_tool_name_rc2(server, admin_user, specs, capsys):
    """도구 이름 규칙 위반(코어 ValueError)·접미사 오류·중복 → rc 2, 기존 승인 그대로."""
    assert await run(
        ["set", "--server", "demo", "--actor-email", admin_user.email, "word_count"]
    ) == 0
    capsys.readouterr()

    rc = await run(["set", "--server", "demo", "--actor-email", admin_user.email, *specs])

    assert rc == 2
    assert capsys.readouterr().err.startswith("❌")
    assert await _approvals() == [("word_count", True)]


@pytest.mark.parametrize("who", ["regular-user", "missing", "inactive-admin"])
async def test_non_admin_actor_rc2(server, user_a, admin_user, db, who, capsys):
    """비인가 관점: admin이 아닌(없거나 비활성인) actor → rc 2, 쓰기·감사 0."""
    email = {"regular-user": user_a.email, "missing": "nobody@test.dev"}.get(who)
    if who == "inactive-admin":
        admin_user.is_active = False
        await db.commit()
        email = admin_user.email

    rc = await run(["set", "--server", "demo", "--actor-email", email, "word_count"])

    assert rc == 2
    assert "actor가 활성 admin이 아닙니다" in capsys.readouterr().err
    assert await _approvals() == []
    async with async_session_factory() as s:
        assert (await s.execute(select(AuditLog))).scalars().all() == []


async def test_unknown_server_rc2(admin_user, capsys):
    """대상 서버가 없으면 set·show 모두 rc 2."""
    assert await run(["set", "--server", "nope", "--actor-email", admin_user.email, "t"]) == 2
    assert await run(["show", "--server", "nope"]) == 2
    assert "MCP 서버가 없습니다: nope" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra", [[], ["--clear", "word_count"]], ids=["no-tools", "clear-with-tools"]
)
async def test_set_requires_tools_or_explicit_clear(server, admin_user, extra, capsys):
    """P2-01: 도구 없는 `set`(인자 누락 실수)과 `--clear`+도구는 사용법 오류 rc 2 — 승인 그대로."""
    actor = ["--actor-email", admin_user.email]
    assert await run(["set", "--server", "demo", *actor, "word_count"]) == 0
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        await run(["set", "--server", "demo", *actor, *extra])

    assert exc.value.code == 2
    assert "--clear" in capsys.readouterr().err
    assert await _approvals() == [("word_count", True)]


async def test_set_clear_removes_all_with_audit(server, admin_user, capsys):
    """P2-01: 전부 지우기는 명시 `--clear`로만 — 승인 0행, 감사 `mcp.tools_approve`(removed)."""
    actor = ["--actor-email", admin_user.email]
    assert await run(["set", "--server", "demo", *actor, "word_count", "echo_upper"]) == 0

    assert await run(["set", "--server", "demo", *actor, "--clear"]) == 0

    assert "승인 0개로 교체" in capsys.readouterr().out
    assert await _approvals() == []
    async with async_session_factory() as s:
        audits = (
            await s.execute(
                select(AuditLog)
                .where(AuditLog.action == "mcp.tools_approve")
                .order_by(AuditLog.created_at)
            )
        ).scalars().all()
    assert sorted(audits[-1].detail["removed"]) == ["echo_upper", "word_count"]


# ---- 스펙 고정(코어 v0.21.0 #293, core-v0.22.0-upgrade AC-4) ----

LIST_TOOLS = "biznotes_core.services.agents.mcp_client.list_tools"


def _listed(name: str, description: str) -> dict:
    """코어 `mcp_client.list_tools` 항목 형태(`input_schema`)."""
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }


async def _pins() -> dict[str, str | None]:
    async with async_session_factory() as s:
        rows = (await s.execute(select(MCPServerTool))).scalars()
        return {r.tool_name: r.approved_spec_hash for r in rows}


async def test_show_live_prints_hash_and_set_pins_it(server, admin_user, monkeypatch, capsys):
    """`show --live`의 해시를 `TOOL=HASH`로 넘기면 고정되고 `approved`가 된다. set은 list_tools를
    다시 부르지 않는다(검토한 스펙만 고정)."""
    from biznotes_core.services.agents.mcp_tool_spec import mcp_tool_spec_hash

    word = _listed("word_count", "단어 수")
    calls = {"n": 0}

    async def fake_list_tools(srv):
        calls["n"] += 1
        return [word]

    monkeypatch.setattr(LIST_TOOLS, fake_list_tools)
    actor = ["--actor-email", admin_user.email]
    digest = mcp_tool_spec_hash(word)

    assert await run(["show", "--server", "demo", "--live"]) == 0
    assert f"word_count\t{digest}\tnot_approved" in capsys.readouterr().out

    assert await run(["set", "--server", "demo", *actor, f"word_count:ro={digest}"]) == 0
    out = capsys.readouterr().out
    assert "✅ demo: 승인 1개로 교체" in out and "⚠️" not in out
    assert await _pins() == {"word_count": digest}
    assert calls["n"] == 1  # set은 list_tools를 부르지 않았다

    assert await run(["show", "--server", "demo", "--live"]) == 0
    assert f"word_count\t{digest}\tapproved" in capsys.readouterr().out
    assert await run(["show", "--server", "demo"]) == 0
    assert capsys.readouterr().out.splitlines()[1].endswith(f"\t{digest}")


async def test_set_without_hash_warns_and_leaves_tool_unpinned(server, admin_user, capsys):
    """해시 없이 승인한 새 `:ro` 도구는 고정되지 않는다 — 노출되지 않는다는 경고를 낸다."""
    actor = ["--actor-email", admin_user.email]
    assert await run(["set", "--server", "demo", *actor, "word_count", "echo_upper:rw"]) == 0

    out = capsys.readouterr().out
    assert "⚠️ 스펙 해시가 고정되지 않은 도구는 노출되지 않습니다: word_count" in out
    assert await _pins() == {"word_count": None, "echo_upper": None}

    from biznotes_core.services.agents.mcp_exposure import approved_tool_pins

    async with async_session_factory() as s:
        assert await approved_tool_pins(s, [server.id]) == {}


@pytest.mark.parametrize("bad", ["ABC", "a" * 63, "g" * 64], ids=["upper", "short", "non-hex"])
async def test_set_bad_hash_rc2(server, admin_user, bad, capsys):
    """해시 형식(sha256 소문자 hex) 위반은 코어 ValueError → rc 2, 쓰기 0."""
    rc = await run(
        ["set", "--server", "demo", "--actor-email", admin_user.email, f"word_count={bad}"]
    )
    assert rc == 2
    assert capsys.readouterr().err.startswith("❌")
    assert await _approvals() == []


async def test_show_live_list_failure_rc1_and_requires_server(server, monkeypatch, capsys):
    async def failing(srv):
        raise ConnectionError("refused secret-ish detail")

    monkeypatch.setattr(LIST_TOOLS, failing)
    assert await run(["show", "--server", "demo", "--live"]) == 1
    err = capsys.readouterr().err
    assert "도구 목록 조회 실패 (ConnectionError)" in err and "secret-ish" not in err

    with pytest.raises(SystemExit) as exc:
        await run(["show", "--live"])
    assert exc.value.code == 2
