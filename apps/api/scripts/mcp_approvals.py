"""MCP 도구 승인 CLI — 승인 정본 `mcp_server_tools`의 조회·전체 교체 (core-v0.11.0-upgrade AC-2.6).

코어 v0.10.13(#139 B)부터 MCP 도구는 관리자 승인 행(`read_only=True`)이 있어야 노출·실행된다.
제품에는 승인 API·UI가 아직 없으므로(HCI-4 (a), BL) 운영자는 이 CLI로 승인한다. 쓰기는 코어
`set_approved_tools`를 그대로 쓴다 — 이름 규칙·서버 행 잠금·감사(`mcp.tools_approve`)를 상속한다.

사용(컨테이너 안):

    python -m scripts.mcp_approvals show [--server NAME]
    python -m scripts.mcp_approvals show --server NAME --live
    python -m scripts.mcp_approvals set --server NAME --actor-email EMAIL TOOL[:ro|:rw][=HASH] ...
    python -m scripts.mcp_approvals set --server NAME --actor-email EMAIL --clear

- `set`은 **전체 교체**다 — 나열하지 않은 기존 승인은 지운다. 접미사가 없으면 `:ro`(read_only).
  도구는 최소 1개를 요구한다 — 인자 누락이 「승인 전부 삭제」가 되지 않게, 전부 지우기는 명시
  플래그 `--clear`로만 한다(도구와 함께 쓸 수 없다). Phase 2 리뷰 P2-01.
  `:rw`(read_only=False)는 노출하지 않고 `mcp_tool` 노드에서도 항상 승인 게이트를 건다(코어).
- actor는 활성 admin이어야 한다.
- **스펙 고정(코어 v0.21.0 #293)**: 코어는 승인 시점 스펙 해시(`approved_spec_hash`)가 현재 스펙과
  같은 도구만 노출한다. `show --live`가 서버에 `list_tools`를 한 번 불러 도구마다 현재 해시와 승인
  상태(`approved`·`unpinned`·`changed`·`not_approved`, 관리 화면 헬스체크와 같은 판정)를 출력한다.
  운영자는 그 스펙을 검토한 뒤 `set`에 `TOOL=HASH`로 넘긴다 — `set`은 `list_tools`를 다시 부르지
  않는다(검토하지 않은 스펙이 고정되지 않게, 코어 `set_approved_tools` 권고). 해시 없이 승인한 새
  `:ro` 도구는 **노출되지 않고**(경고 출력), 기존 행은 고정값을 그대로 둔다. 업그레이드 전 승인은
  모두 `unpinned`라 다시 승인해야 한다(docs/plans/core-v0.22.0-upgrade.md).

종료 코드: 0 성공 · 1 `--live` 서버 조회 실패 · 2 입력 오류(서버 없음·admin 아님·도구 이름 규칙
위반·접미사 오류·해시 형식 오류·도구 없이
`set`·`--clear`와 도구 동시 지정 — 마지막 둘은 argparse 사용법 오류로 `SystemExit(2)`).
"""

import argparse
import asyncio
import sys

from biznotes_core.core.db import async_session_factory
from biznotes_core.models import MCPServer, MCPServerTool, User
from biznotes_core.services.agents.mcp_exposure import set_approved_tools, tool_pin_states
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.mcp_pins import pin_state, spec_hash_or_none

_SUFFIX = {"ro": True, "rw": False}


def _parse_tools(specs: list[str]) -> tuple[dict[str, bool], dict[str, str]]:
    """`TOOL[:ro|:rw][=HASH]` 목록 → (이름 → read_only, 이름 → 스펙 해시). 접미사 오류·중복은
    ValueError. 해시 형식은 코어 `set_approved_tools`가 검사한다(sha256 소문자 hex)."""
    tools: dict[str, bool] = {}
    hashes: dict[str, str] = {}
    for spec in specs:
        head, has_hash, spec_hash = spec.partition("=")
        name, sep, suffix = head.rpartition(":")
        if not sep:
            name, suffix = head, "ro"
        if suffix not in _SUFFIX:
            raise ValueError(f"접미사는 :ro 또는 :rw여야 합니다: {spec}")
        if name in tools:
            raise ValueError(f"같은 도구가 두 번 있습니다: {name}")
        tools[name] = _SUFFIX[suffix]
        if has_hash:
            hashes[name] = spec_hash
    return tools, hashes


async def _server_by_name(db: AsyncSession, server_name: str) -> MCPServer | None:
    return (
        await db.execute(select(MCPServer).where(MCPServer.name == server_name))
    ).scalar_one_or_none()


async def _show_live(db: AsyncSession, server: MCPServer) -> int:
    """서버의 현재 도구마다 스펙 해시·승인 상태 — `set TOOL=HASH`에 넘길 값을 보여준다."""
    from biznotes_core.services.agents.mcp_client import list_tools

    try:
        tools = await list_tools(server)
    except Exception as e:  # noqa: BLE001 — 연결 실패는 종료 코드 1로 알린다
        print(f"❌ {server.name}: 도구 목록 조회 실패 ({type(e).__name__})", file=sys.stderr)
        return 1
    pins = (await tool_pin_states(db, [server.id])).get(server.id, {})
    print("tool\tspec_hash\tapproval")
    for tool in tools:
        name = str(tool.get("name") or "")
        spec_hash = spec_hash_or_none(tool)
        state = pin_state(name, spec_hash, pins)
        print(f"{name}\t{spec_hash or '-(거부 — 고정 불가)'}\t{state}")
    print(f"도구 {len(tools)}개")
    return 0


async def _show(db: AsyncSession, server_name: str | None, *, live: bool = False) -> int:
    query = (
        select(MCPServer.name, MCPServerTool.tool_name, MCPServerTool.read_only, User.email,
               MCPServerTool.approved_at, MCPServerTool.approved_spec_hash)
        .join(MCPServerTool, MCPServerTool.server_id == MCPServer.id)
        .outerjoin(User, User.id == MCPServerTool.approved_by)
        .order_by(MCPServer.name, MCPServerTool.tool_name)
    )
    if server_name is not None:
        server = await _server_by_name(db, server_name)
        if server is None:
            print(f"❌ MCP 서버가 없습니다: {server_name}", file=sys.stderr)
            return 2
        if live:
            return await _show_live(db, server)
        query = query.where(MCPServer.name == server_name)
    rows = (await db.execute(query)).all()
    print("server\ttool\tread_only\tapproved_by\tapproved_at\tspec_hash")
    for name, tool, read_only, email, approved_at, spec_hash in rows:
        flag = str(read_only).lower()
        pinned = spec_hash or "-(미고정)"
        print(f"{name}\t{tool}\t{flag}\t{email or '-'}\t{approved_at.isoformat()}\t{pinned}")
    print(f"승인 {len(rows)}건")
    return 0


async def _set(db: AsyncSession, server_name: str, actor_email: str, specs: list[str]) -> int:
    actor = (await db.execute(select(User).where(User.email == actor_email))).scalar_one_or_none()
    if actor is None or actor.role != "admin" or not actor.is_active:
        print(f"❌ actor가 활성 admin이 아닙니다: {actor_email}", file=sys.stderr)
        return 2
    server = await _server_by_name(db, server_name)
    if server is None:
        print(f"❌ MCP 서버가 없습니다: {server_name}", file=sys.stderr)
        return 2
    try:
        tools, hashes = _parse_tools(specs)
        await set_approved_tools(db, server, tools, admin_id=actor.id, spec_hashes=hashes)
    except ValueError as e:
        await db.rollback()
        print(f"❌ {e}", file=sys.stderr)
        return 2
    await db.commit()
    print(f"✅ {server_name}: 승인 {len(tools)}개로 교체 ({', '.join(sorted(tools)) or '없음'})")
    pins = (await tool_pin_states(db, [server.id])).get(server.id, {})
    unpinned = sorted(name for name, spec_hash in pins.items() if spec_hash is None)
    if unpinned:
        print(
            f"⚠️ 스펙 해시가 고정되지 않은 도구는 노출되지 않습니다: {', '.join(unpinned)}"
            " — `show --server NAME --live`의 해시로 `TOOL=HASH` 승인하세요"
        )
    return 0


async def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mcp_approvals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="승인 목록")
    show.add_argument("--server")
    show.add_argument(
        "--live", action="store_true", help="서버에 list_tools를 불러 현재 스펙 해시·승인 상태 출력"
    )
    setp = sub.add_parser("set", help="승인 전체 교체")
    setp.add_argument("--server", required=True)
    setp.add_argument("--actor-email", required=True)
    # 「최소 1개」는 nargs="+"와 같은 규칙이다. `--clear`일 때만 비울 수 있어야 하므로 nargs="*"로
    # 받고 아래에서 둘 중 정확히 하나를 강제한다(argparse 사용법 오류 = rc 2).
    setp.add_argument("tools", nargs="*", metavar="TOOL[:ro|:rw][=HASH]")
    setp.add_argument("--clear", action="store_true", help="이 서버의 승인을 모두 지운다")
    args = parser.parse_args(argv)
    if args.command == "show" and args.live and args.server is None:
        show.error("--live에는 --server가 필요하다")
    if args.command == "set" and args.clear == bool(args.tools):
        setp.error("도구를 1개 이상 주거나, 승인을 모두 지우려면 도구 없이 --clear를 준다")

    async with async_session_factory() as db:
        if args.command == "show":
            return await _show(db, args.server, live=args.live)
        return await _set(db, args.server, args.actor_email, args.tools)


def main() -> None:
    sys.exit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
