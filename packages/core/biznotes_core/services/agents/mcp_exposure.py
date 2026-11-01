"""MCP 노출 판정과 차단 신호 (#139, docs/plans/mcp-exposure/ 단계 C).

`mcp_exposure_for`는 디스커버리(`tool_registry._discover_mcp_tools`)가 서버를 노출하는 규칙을 서버
하나·기준(`audience`) 하나에 대해 계산한다. 관리자 API가 `audience="user"`로 부르면 일반 사용자 실행
기준 노출을 얻는다 — 관리자 세션 기준 "N개 노출"이 일반 사용자 실행과 달라 운영자가 잘못 판단하던
문제를 푼다.

`unavailable_mcp_tools`는 react·llm 노드가 허용한 MCP 도구가 이 실행에서 막힌 사유를 낸다. 루프는
사유를 `RunContext.unavailable_tools`에 남기고 react 노드의 LLM에 조건부 조항을 붙인다. 도구
이벤트는 내지 않는다. 그래서 LLM이 권한 차단을 "연결 안 됨, 설정 확인"으로, 장애를 권한 문제로
잘못 안내하지 않는다.

승인 도구의 정본은 DB(`MCPServerTool`, 단계 B)다. `approved_tools`·`approved_tool_pins`가 판정에
쓰는 읽기이고, `set_approved_tools`가 관리자 API가 부르는 쓰기다. 둘 다 호출측 세션에서 돌고
커밋은 호출측이 한다. 승인은 「read-only + 승인 시점 스펙 고정(`approved_spec_hash`)」이다(#293).
고정되지 않은 행은 LLM 노출·호출 경로에서 승인으로 보지 않는다(관리자 정의의 `mcp_tool` 노드는
예외).
DB 오류는 삼키지 않는다 — run 세션에서 실패를 삼키면 PostgreSQL 트랜잭션이 aborted로 남아 이후
쿼리가 모두 실패한다(리뷰 B-2). "fail-closed"는 승인 행이 없으면 노출하지 않는다는 뜻이다.
"""

import re
import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from biznotes_core.models import MCPServer
    from biznotes_core.services.agents.context import RunContext

logger = structlog.get_logger()

MCPExposureReason = Literal["ok", "role_denied", "server_disabled", "no_approved_tools"]
MCPAudience = Literal["admin", "user"]
# 실행에서 MCP 도구가 막힌 사유. 역할이 통과했는데 노출이 0개면 디스커버리 결과로 나눈다 —
# 켜진 서버가 없다(`no_servers`), 승인된 서버가 응답하지 않았다(`discovery_failed`), 승인된 도구가
# 없다(`no_approved_tools`). 장애를 권한 문제로 안내하지 않으려는 구분이다(리뷰 C-3).
MCPUnavailableReason = Literal["role_denied", "no_servers", "no_approved_tools", "discovery_failed"]

# 루프가 react 노드의 LLM system 메시지에 붙이는 조항(사유별). 일반 사용자 채팅마다 붙으므로
# 사용자가 그 도구를 요청했을 때만 안내하게 한다(리뷰 C-4).
_ONLY_WHEN_ASKED = (
    "사용자가 외부 MCP 서버의 정보나 도구를 요청한 경우에만 이 사실을 안내하고, "
    "그렇지 않으면 언급하지 마십시오."
)
MCP_UNAVAILABLE_CLAUSES: dict[MCPUnavailableReason, str] = {
    "role_denied": (
        "참고: 이 실행에서는 권한 정책 때문에 MCP 도구(외부 MCP 서버의 도구)를 "
        "쓸 수 없습니다. 연결이나 설정 문제가 아니므로 설정 파일을 확인하라고 하지 말고, "
        "관리자에게 문의하라고 안내하십시오. " + _ONLY_WHEN_ASKED
    ),
    "no_servers": (
        "참고: 이 실행에서 쓸 수 있는(켜진) MCP 서버가 없습니다. 관리자에게 서버 등록이나 "
        "활성화를 문의하라고 안내하십시오. " + _ONLY_WHEN_ASKED
    ),
    "no_approved_tools": (
        "참고: 이 실행에서 쓸 수 있도록 승인된 MCP 도구가 없습니다. 연결이나 설정 파일 "
        "문제로 단정하지 말고, 관리자의 도구 승인이 필요하다고 안내하십시오. " + _ONLY_WHEN_ASKED
    ),
    "discovery_failed": (
        "참고: 승인된 MCP 서버가 지금 응답하지 않아 MCP 도구를 쓸 수 없습니다. 일시적인 연결 "
        "문제일 수 있으니 잠시 뒤 다시 시도하거나 관리자에게 서버 상태 확인을 요청하라고 "
        "안내하십시오. " + _ONLY_WHEN_ASKED
    ),
}


def mcp_audience(user_role: str, user_id: "uuid.UUID | None") -> MCPAudience:
    """실행의 MCP 노출 기준 — 관리자와 사용자 없는 시스템 실행은 `admin`, 그 밖은 `user`다.

    `admin`은 켜진 모든 서버, `user`는 관리자가 공개(`exposure="all_users"`)한 서버만 본다(#139 A).
    어느 쪽이든 승인된 read-only 도구만 노출한다.
    """
    from biznotes_core.services.agents.tool_registry import _is_admin_audience

    return "admin" if _is_admin_audience(user_role, user_id) else "user"


@dataclass(frozen=True)
class MCPExposure:
    """서버 하나의 노출 판정. `tools`는 승인된 도구 이름이다.

    실제 노출은 디스커버리(`list_tools`)가 돌려준 이름과 `tools`의 교집합에서 스펙이 거부된 도구
    (`mcp_tool_spec.mcp_tool_function`, #229)를 뺀 것이다 — 서버가 응답하지 않으면 승인돼 있어도
    노출되지 않는다. 노출되지 않으면 `tools`는 비어 있다.
    """

    exposed: bool
    reason: MCPExposureReason
    tools: frozenset[str] = frozenset()


async def mcp_exposure_for(
    db: "AsyncSession", server: "MCPServer", *, audience: MCPAudience
) -> MCPExposure:
    """디스커버리와 같은 규칙으로 서버 하나가 이 기준(`audience`)의 실행에 노출되는지 판정한다.

    관리자 API는 `audience="user"`로 일반 사용자 실행 기준 노출을 얻는다. 실행 ctx에서 고를 때는
    `mcp_audience(ctx.user_role, ctx.user_id)`를 쓴다. `user_id=None`을 "일반 사용자"로 오인하지
    않도록 역할·id 대신 기준을 받는다(리뷰). 판정 순서는 디스커버리와 같다 — 꺼진 서버 제외 → 기준
    → 승인 도구. `tools`는 **승인된 도구 집합**이고, 실제 노출은 디스커버리(`list_tools`)가 돌려준
    이름과의 교집합이다. 승인 도구는 DB에서 읽는다(쿼리 1회).
    """
    if not server.enabled:
        return MCPExposure(exposed=False, reason="server_disabled")
    if audience != "admin" and server.exposure != "all_users":
        return MCPExposure(exposed=False, reason="role_denied")
    tools = (await approved_tools(db, [server.id])).get(server.id, frozenset())
    if not tools:
        return MCPExposure(exposed=False, reason="no_approved_tools")
    return MCPExposure(exposed=True, reason="ok", tools=tools)


async def unavailable_mcp_tools(
    ctx: "RunContext", allowed: Iterable[str] | None, exposed: Collection[str]
) -> dict[str, MCPUnavailableReason]:
    """허용 목록의 MCP 도구(`mcp`·`mcp_call`)가 이 실행에서 막힌 사유. 막힌 것이 없으면 빈 dict다.

    `exposed`는 `build_tool_specs`가 실제로 노출한 도구 이름(디스패처 키)이다. 디스커버리 결과를
    다시 계산하지 않고 그 결과로 "역할은 통과했지만 0개"를 판정한다.
    """
    from biznotes_core.services.agents.tool_registry import MCP_TOOL_PREFIX

    names = set(allowed or ())
    audience = mcp_audience(ctx.user_role, ctx.user_id)
    out: dict[str, MCPUnavailableReason] = {}
    if "mcp" in names and not any(
        n.startswith(MCP_TOOL_PREFIX) and n != "mcp_call" for n in exposed
    ):
        outcome = getattr(ctx, "_mcp_discovery_outcome", None)
        if audience == "user" and not (outcome or {}).get("servers"):
            # 일반 사용자에게 공개된 서버가 없다 — admin_only 서버의 존재·상태는 사유에 넣지 않는다
            # (근거 수치도 공개 서버만 센다, 리뷰 A-2)
            out["mcp"] = "role_denied"
        else:
            out["mcp"] = _empty_discovery_reason(outcome)
    if "mcp_call" in names and "mcp_call" not in exposed:
        out["mcp_call"] = "role_denied"
    return out


MCPCallVerdict = Literal["ok", "missing", "not_public", "not_approved"]
# 일반 사용자에게 주는 단일 거부 메시지 — 서버 존재·공개 범위·승인 여부를 가르지 않는다(#139 A)
USER_MCP_UNAVAILABLE = "이 실행에서 사용할 수 없는 MCP 도구입니다"


async def check_mcp_call(
    db: "AsyncSession", server_id: uuid.UUID, tool_name: str, *, audience: MCPAudience
) -> "tuple[MCPServer | None, MCPCallVerdict]":
    """MCP 도구 호출 직전 재확인(#139 A-4) — 서버 활성, 공개 범위(일반 사용자), 승인 read-only.

    서버 행이 없을 때만 `None`이다. 판정이 `ok`가 아니면 호출하지 않는다.

    서버 행은 `populate_existing=True`로 다시 읽는다. 디스커버리가 같은 세션 identity map에 올려 둔
    옛 객체를 쓰면 다른 트랜잭션의 비활성·`admin_only` 전환을 놓친다(재리뷰 A Major 1). 승인은 매번
    새로 조회한다. 그래서 run 캐시·HITL 재개가 이 판정을 우회하지 못한다.
    """
    from biznotes_core.models import MCPServer

    server = await db.get(MCPServer, server_id, populate_existing=True)
    if server is None:
        return None, "missing"
    if not server.enabled:
        # 꺼진 서버도 행은 돌려준다 — 실패 evidence에 서버 이름이 남는다(기존 동작)
        return server, "missing"
    if audience != "admin" and server.exposure != "all_users":
        return server, "not_public"
    if tool_name not in (await approved_tools(db, [server.id])).get(server.id, frozenset()):
        return server, "not_approved"
    return server, "ok"


def mcp_call_denial_message(verdict: MCPCallVerdict, audience: MCPAudience) -> str:
    """거부 메시지 — 관리자·시스템 실행은 기존 사유별 문구, 일반 사용자는 단일 문구."""
    if audience != "admin":
        return USER_MCP_UNAVAILABLE
    if verdict == "not_approved":
        return "MCP 도구가 read-only 승인 목록에 없습니다"
    return "MCP 서버를 찾을 수 없거나 비활성화됨"


async def user_mcp_tools_available(db: "AsyncSession") -> bool:
    """켜진 공개(`all_users`) 서버에 승인 read-only 도구가 있는가 — `mcp_call` 노출 기준."""
    from sqlalchemy import select

    from biznotes_core.models import MCPServer

    rows = await db.execute(
        select(MCPServer.id).where(
            MCPServer.enabled.is_(True), MCPServer.exposure == "all_users"
        )
    )
    server_ids = list(rows.scalars().all())
    return bool(await approved_tools(db, server_ids))


# 감사 detail의 인자 키 상한 — 키는 LLM이 정한 문자열이다(값은 남기지 않는다)
_ARG_KEYS_MAX = 20
_ARG_KEY_LEN = 64


def _arg_keys(arguments: Any) -> list[str]:
    if not isinstance(arguments, dict):
        return []
    return sorted(str(k)[:_ARG_KEY_LEN] for k in arguments)[:_ARG_KEYS_MAX]


async def _audit_isolated(
    ctx: "RunContext", action: str, server: "MCPServer", detail: dict[str, Any]
) -> None:
    """run 트랜잭션과 분리된 세션에 감사 행을 커밋한다 — 실패한 run의 롤백에 휩쓸리지 않는다."""
    from biznotes_core.core.db import async_session_factory
    from biznotes_core.services.audit import record

    factory = ctx.session_factory or async_session_factory
    async with factory() as session:
        await record(session, ctx.user_id, action, server.name, detail)
        await session.commit()


async def audit_user_mcp_call(
    ctx: "RunContext", server: "MCPServer", tool_name: str, arguments: Any
) -> uuid.UUID:
    """일반 사용자 실행의 MCP 호출 **전에** 감사 행(`mcp.call`, started)을 커밋한다(#139 A-3).

    기록에 실패하면 호출하지 않는다 — 일반 메시지의 `ToolUnavailableError`를 올린다. 돌려준 id는
    결과 행(`mcp.call_result`)과 짝짓는 상관 id다. 인자 값은 남기지 않는다(키만, 상한 있음).
    """
    from biznotes_core.services.agents.errors import ToolUnavailableError

    call_id = uuid.uuid4()
    detail = {
        "call_id": str(call_id),
        "run_id": str(ctx.run_id),
        "server_id": str(server.id),
        "tool": tool_name,
        "arg_keys": _arg_keys(arguments),
        "status": "started",
    }
    try:
        await _audit_isolated(ctx, "mcp.call", server, detail)
    except Exception as e:
        logger.warning(
            "MCP 호출 감사 기록 실패 — 호출하지 않음", run_id=str(ctx.run_id), error=str(e)
        )
        raise ToolUnavailableError(USER_MCP_UNAVAILABLE) from e
    return call_id


async def audit_user_mcp_result(
    ctx: "RunContext", server: "MCPServer", tool_name: str, status: str, call_id: uuid.UUID
) -> None:
    """호출 결과를 별도 행(`mcp.call_result`)으로 남긴다(best-effort, 시작 행은 고치지 않는다)."""
    detail = {
        "call_id": str(call_id),
        "run_id": str(ctx.run_id),
        "server_id": str(server.id),
        "tool": tool_name,
        "status": status,
    }
    try:
        await _audit_isolated(ctx, "mcp.call_result", server, detail)
    except Exception as e:  # noqa: BLE001 — 결과 기록 실패가 이미 끝난 호출을 바꾸지 않는다
        logger.warning("MCP 호출 결과 감사 기록 실패", run_id=str(ctx.run_id), error=str(e))


async def set_server_exposure(
    db: "AsyncSession", server: "MCPServer", exposure: str, *, admin_id: uuid.UUID | None
) -> None:
    """서버 공개 범위를 바꾼다(`admin_only` | `all_users`). 커밋은 호출측이 한다.

    바뀌면 감사 `mcp.exposure_change`를 남긴다. 실행 중인 run도 다음 호출부터 새 값을 본다
    (`check_mcp_call`).
    """
    from biznotes_core.models.agent import MCP_SERVER_EXPOSURES
    from biznotes_core.services.audit import record

    if exposure not in MCP_SERVER_EXPOSURES:
        raise ValueError(f"MCP 서버 공개 범위가 아닙니다: {exposure!r}")
    if server.exposure == exposure:
        return
    before = server.exposure
    server.exposure = exposure
    await db.flush()
    await record(
        db,
        admin_id,
        "mcp.exposure_change",
        server.name,
        {"server_id": str(server.id), "from": before, "to": exposure},
    )


# MCP 도구 이름 규칙(영문·숫자·`_`·`.`·`-`, 1~128자). `/`·공백은 받지 않는다.
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")


async def _read_only_pins(
    db: "AsyncSession", server_ids: Collection[uuid.UUID], *, pinned_only: bool
) -> dict[uuid.UUID, dict[str, str | None]]:
    from sqlalchemy import select

    from biznotes_core.models import MCPServerTool

    if not server_ids:
        return {}
    conditions = [
        MCPServerTool.server_id.in_(list(server_ids)),
        MCPServerTool.read_only.is_(True),
    ]
    if pinned_only:
        conditions.append(MCPServerTool.approved_spec_hash.is_not(None))
    rows = await db.execute(
        select(
            MCPServerTool.server_id, MCPServerTool.tool_name, MCPServerTool.approved_spec_hash
        ).where(*conditions)
    )
    grouped: dict[uuid.UUID, dict[str, str | None]] = {}
    for server_id, tool_name, spec_hash in rows.tuples().all():
        grouped.setdefault(server_id, {})[tool_name] = spec_hash
    return grouped


async def tool_pin_states(
    db: "AsyncSession", server_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, dict[str, str | None]]:
    """서버별 read-only 승인 행의 고정 해시 — 고정되지 않은 행은 `None`이다(#293). 쿼리 1회.

    관리 화면이 승인 상태를 셋으로 나눌 때 쓴다: `None`이면 「미고정」(재승인 필요), 현재
    `mcp_tool_spec_hash`와 다르면 「승인 뒤 변경됨」, 같으면 「승인」. 저장 판정도 같은 기준을 쓴다.
    """
    return await _read_only_pins(db, server_ids, pinned_only=False)


async def approved_tool_pins(
    db: "AsyncSession", server_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, dict[str, str]]:
    """서버별 노출 승인 도구와 승인 시점 스펙 해시(#293). 쿼리 1회, 빈 입력이면 쿼리하지 않는다.

    `read_only=True`이고 `approved_spec_hash`가 있는 행만 담는다. 디스커버리는 현재 스펙의
    `mcp_tool_spec_hash`가 이 값과 다르면 그 도구를 노출하지 않는다. 승인 행이 없는 서버는 결과에
    없다. DB 오류는 그대로 올린다(모듈 docstring).
    """
    rows = await _read_only_pins(db, server_ids, pinned_only=True)
    return {
        server_id: {name: spec_hash for name, spec_hash in pins.items() if spec_hash is not None}
        for server_id, pins in rows.items()
    }


async def approved_tools(
    db: "AsyncSession", server_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, frozenset[str]]:
    """서버별 노출 승인 도구 이름. 쿼리 1회, 빈 입력이면 쿼리하지 않는다.

    `approved_tool_pins`와 같은 조건이다 — `read_only=True`이고 승인 시점 스펙이 고정된 행만(#293).
    고정되지 않은 승인(이 규칙 이전의 승인·시드가 직접 만든 행)은 다시 승인할 때까지 LLM 노출·호출
    경로(디스커버리, 호출 직전 재확인, 노출 신호)에서 승인으로 보지 않는다. 이 경로들은 고정 행이
    있는지만 본다 — 스펙 변경(해시 불일치)은 디스커버리 노출만 막는다(설명이 LLM에 가는 곳).
    관리자 정의의 `mcp_tool` 노드는 이 함수를 쓰지 않는다. 승인 행이 없는 서버는 결과에 없다.
    """
    pins = await approved_tool_pins(db, server_ids)
    return {server_id: frozenset(names) for server_id, names in pins.items()}


# 승인 시점 스펙 해시(`mcp_tool_spec_hash`) — sha256 소문자 hex
_SPEC_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _approval_pins(
    tools: Mapping[str, bool], spec_hashes: Mapping[str, str] | None
) -> dict[str, str]:
    """승인과 함께 고정할 해시를 검사한다(#293). 승인하지 않는 이름·형식 오류는 `ValueError`."""
    pins = dict(spec_hashes or {})
    unknown = sorted(str(n) for n in pins if n not in tools)
    if unknown:
        raise ValueError(f"승인하지 않는 MCP 도구의 스펙 해시입니다: {unknown}")
    bad = sorted(
        n for n, h in pins.items() if not isinstance(h, str) or not _SPEC_HASH_RE.fullmatch(h)
    )
    if bad:
        raise ValueError(f"MCP 도구 스펙 해시 형식(sha256 소문자 hex)이 아닙니다: {bad}")
    return pins


async def set_approved_tools(
    db: "AsyncSession",
    server: "MCPServer",
    tools: Mapping[str, bool],
    *,
    admin_id: uuid.UUID | None,
    spec_hashes: Mapping[str, str] | None = None,
) -> None:
    """서버의 승인 도구를 `이름 → read_only`로 **전체 교체**한다. 커밋은 호출측이 한다.

    서버 행을 `FOR UPDATE`로 잠근 **뒤에** 기존 승인을 읽는다 — READ COMMITTED에서 앞선 교체의
    커밋을 보고, 동시 교체가 UNIQUE 충돌이나 lost update를 내지 않는다. 그대로인 항목은
    `approved_by`·`approved_at`을 유지한다(처음 승인한 사람·시각). 이름은 MCP 도구 이름 규칙만
    검사하고(어기면 `ValueError`), 헬스체크로 발견한 이름인지는 제품 API가 판단한다. 바뀐 것이
    있으면 감사 `mcp.tools_approve`를 남긴다.

    `spec_hashes`는 관리자가 승인 화면에서 **검토한** 스펙의 해시다(이름 → `mcp_tool_spec_hash`,
    #293). 제품은 헬스체크·도구 목록 응답에 도구별 해시를 싣고 승인 요청이 그 값을 되돌려 보낸다 —
    저장할 때 `list_tools`를 다시 불러 고정하면 첫 조회와 다른 스펙을 주는 서버가 관리자가 보지
    않은 스펙을 고정시킬 수 있다. 고정되지 않은 승인은 노출되지 않는다. 해시가 바뀐 기존 행은 다시
    승인한 것으로 보고 `approved_by`·`approved_at`을 갱신하며 감사 detail의 `pinned`에 남긴다(새
    행은 `added`에만 있다). 해시가 없는 이름은 새 행이면 고정되지 않고, 기존 행이면 고정값을 그대로
    둔다. 승인하지 않는 이름이나 해시 형식이 아니면 `ValueError`다.
    """
    from sqlalchemy import select

    from biznotes_core.models import MCPServer, MCPServerTool
    from biznotes_core.services.audit import record

    invalid = sorted(n for n in tools if not isinstance(n, str) or not _TOOL_NAME_RE.fullmatch(n))
    if invalid:
        raise ValueError(f"MCP 도구 이름 형식이 아닙니다: {invalid}")
    pins = _approval_pins(tools, spec_hashes)
    await db.execute(select(MCPServer.id).where(MCPServer.id == server.id).with_for_update())
    existing = {
        row.tool_name: row
        for row in (
            await db.execute(
                select(MCPServerTool)
                .where(MCPServerTool.server_id == server.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    now = datetime.now(UTC)
    removed = sorted(set(existing) - set(tools))
    added: list[str] = []
    changed: list[str] = []
    pinned: list[str] = []
    for name in removed:
        await db.delete(existing[name])
    for name, read_only in sorted(tools.items()):
        row = existing.get(name)
        if row is None:
            db.add(
                MCPServerTool(
                    server_id=server.id,
                    tool_name=name,
                    read_only=bool(read_only),
                    approved_by=admin_id,
                    approved_at=now,
                    approved_spec_hash=pins.get(name),
                )
            )
            added.append(name)
            continue
        read_only_changed = row.read_only != bool(read_only)
        repinned = name in pins and row.approved_spec_hash != pins[name]
        if read_only_changed:
            row.read_only = bool(read_only)
            changed.append(name)
        if repinned:
            row.approved_spec_hash = pins[name]
            pinned.append(name)
        if read_only_changed or repinned:
            row.approved_by, row.approved_at = admin_id, now
    await db.flush()
    if added or removed or changed or pinned:
        detail: dict[str, Any] = {
            "server_id": str(server.id),
            "added": added,
            "removed": removed,
            "read_only_changed": changed,
        }
        if pinned:
            detail["pinned"] = pinned  # 스펙을 다시 고정한 기존 도구(#293)
        await record(db, admin_id, "mcp.tools_approve", server.name, detail)


def _empty_discovery_reason(outcome: dict[str, int] | None) -> MCPUnavailableReason:
    """역할은 통과했는데 노출이 0개일 때의 사유. 디스커버리 근거가 없으면 승인 없음으로 본다."""
    if not outcome:
        return "no_approved_tools"
    if outcome.get("servers", 0) == 0:
        return "no_servers"
    if outcome.get("failed_approved_servers", 0) > 0:
        return "discovery_failed"
    return "no_approved_tools"


__all__ = [
    "MCP_UNAVAILABLE_CLAUSES",
    "MCPAudience",
    "MCPExposure",
    "MCPExposureReason",
    "MCPUnavailableReason",
    "USER_MCP_UNAVAILABLE",
    "MCPCallVerdict",
    "approved_tool_pins",
    "approved_tools",
    "audit_user_mcp_call",
    "audit_user_mcp_result",
    "check_mcp_call",
    "mcp_audience",
    "mcp_call_denial_message",
    "mcp_exposure_for",
    "set_approved_tools",
    "set_server_exposure",
    "tool_pin_states",
    "unavailable_mcp_tools",
    "user_mcp_tools_available",
]
