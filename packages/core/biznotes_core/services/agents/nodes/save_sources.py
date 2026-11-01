"""`save_sources` 노드(#403) — 렌더링한 목록(MCP 도구 결과 등)으로 노트북에 소스를 만든다.

계획 `docs/plans/save-sources-node.md`. 정의 예:

    {"id": "save", "type": "save_sources", "items": "{list.text}",
     "namespace": "naver-research", "requires_approval": false,
     "allowed_hosts": ["stock.pstatic.net"],
     "notebook_rule": {"title_template": "종목분석 {month}", "timezone": "Asia/Seoul",
                       "emoji": "📈"}}

- `items`: 템플릿 하나. 렌더링한 값을 `json.loads`하면 배열이어야 한다. 항목은 `SaveSourceItem`이다.
  입력은 앞 `MAX_INPUT_ITEMS`개까지만 본다. 한 실행에서 새로 처리하는(중복이 아닌) 항목은
  `agent_save_sources_max_items`개까지다 — 이미 있는 항목은 한 번의 묶음 조회로 먼저 걸러 세지
  않는다. 그래서 같은 목록을 다시 돌리면 남은 항목이 이어서 처리된다(리뷰 M1). 어느 쪽이든 남긴
  항목이 있으면 `truncated`가 참이고 `total`은 입력 항목 수다.
- `namespace`: 리터럴(렌더링하지 않는다). 중복 키는 `mcp:<namespace>:<external_id>`다.
- 직접 부작용 노드다(`DIRECT_SIDE_EFFECT_NODE_TYPES`): system 정의만 `requires_approval: false`로
  승인 없이 돌고, 그 밖은 승인 게이트다. 승인 카드는 항목 수·앞 5개 제목·호스트·대상만 싣는다.
- 대상: `notebook_rule`이 없으면 run의 노트북(`ctx.notebook_id`)이다. 항목마다 run 사용자의 쓰기
  권한(owner·write)을 다시 보고, 없으면 그 항목이 `forbidden`이다. run 사용자가 없으면 노드가
  실패한다.
- `notebook_rule`이 있으면 run 사용자가 **실제로 소유한** 제목 계열 노트북에 쓴다
  (`services/notebook_series.py`). 제목(`{year}`·`{month}`·`{date}`(YYYY-MM-DD, #434))은 노드
  시작 때 규칙 시간대(없으면 `display_timezone`)로 한 번 계산한다. 이번 기간(해·달·날)의 가장 큰
  `(n)`에 쓰고, 꽉 차면 n+1(≤99)을 run 사용자 명의로 만든다 — 한 실행 중에 상한을 넘겨도 이어
  쓴다. 계열에 없으면 새로 만든다. 스케줄의 노트북은 앵커일 뿐 대상이 아니다(무인 관문은 앵커를
  본다).
- 항목마다 따로 커밋한다 — 한 항목의 실패가 나머지를 막지 않는다. 실패 사유는 고정 코드·문구로
  출력에 남기고(`SAVE_SOURCE_FAILURE_MESSAGES`), 원문은 서버 로그로만 남긴다. 저장 중 예외(원본
  업로드·DB 오류)는 그 항목만 `save_failed`다. lease 상실·취소·승인·정책 예외는 노드를 멈춘다.
- 중복: 범위 안에 같은 키의 소스가 있으면(상태 무관, failed 포함) `skipped_duplicate`다. 범위는
  대상 노트북, 규칙이 있으면 계열 전체(모든 기간·`(n)`)다. 사용자가 지운 소스는 표식이 남지 않아
  다음 실행에서 다시 들어온다.
- `url` 항목(PDF): 노드 리터럴 `allowed_hosts`와 전역 `AGENT_HTTP_ALLOWED_HOSTS`(있으면 교집합)의
  호스트에서만 받는다(`ssrf_safe_request`, 받아오기 전용 정책 — 매 홉 허용 호스트·SSRF 검사).
  허용 호스트가 없으면 받지 않고 `host_not_allowed`다. 상한은 유효 업로드 상한, 항목마다 전체 시간
  한도가 있다. 판정은 `admit_bytes`(`%PDF`)가 하고 content-type은 로그로만 남긴다. 업로드와 같은
  파일 소스(`type="file"`, `application/pdf`, `origin_url`)가 되어 워커가 같은 파서로 읽는다. 받는
  동안 잠금·트랜잭션을 쥐지 않고, 이미 있는 키는 받지 않는다. URL은 출력·로그에 남기지 않는다
  (호스트만).

출력: `{created, skipped_duplicate, failed, notebook_ids, truncated, total, text}`.
"""

import json
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio
import httpx
import structlog
from pydantic import ValidationError

from biznotes_core.config import get_settings
from biznotes_core.core.ssrf import (
    EgressBlockedError,
    EgressPolicy,
    SSRFBlockedError,
    host_matches,
    ssrf_safe_request,
)
from biznotes_core.schemas.source import SAVE_SOURCE_EXTERNAL_ID_PATTERN, SaveSourceItem
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import AgentRuntimeError
from biznotes_core.services.agents.nodes.composite import _assert_lease_valid, _direct_node_gate
from biznotes_core.services.agents.templating import render
from biznotes_core.services.ingestion.source_warnings import clean_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from biznotes_core.services.ingestion.source_create import SourceAdmission

logger = structlog.get_logger()

# 항목 실패 코드와 고정 문구 — 출력 계약이다(코드 추가만 한다)
SAVE_SOURCE_FAILURE_MESSAGES: dict[str, str] = {
    "invalid_item": "항목 형식이 올바르지 않습니다",
    "host_not_allowed": "허용되지 않은 호스트의 주소입니다",
    "ssrf_blocked": "보안 정책으로 막힌 주소입니다",
    "fetch_failed": "원본을 받아 오지 못했습니다",
    "too_large": "원본이 크기 상한을 넘습니다",
    "not_pdf": "원본이 PDF가 아닙니다",
    "notebook_full": "노트북의 소스 상한에 닿았습니다",
    "forbidden": "대상 노트북에 쓸 권한이 없습니다",
    "save_failed": "소스를 저장하지 못했습니다",
}

_TITLE_MAX_CHARS = 500
_CARD_ITEMS = 5
_CARD_TITLE_CHARS = 100
_CARD_HOST_CHARS = 253
# 승인 카드 전체 상한(JSON 글자 수) — 넘으면 목록을 뒤에서부터 줄인다
_CARD_MAX_CHARS = 4000
_EXTERNAL_ID_RE = re.compile(SAVE_SOURCE_EXTERNAL_ID_PATTERN)
# 한 실행에서 보는 입력 항목 수의 상한 — 새로 처리할 항목 상한(설정, ≤1000)과 별개다
MAX_INPUT_ITEMS = 5000
_PDF_MIME = "application/pdf"
# URL 항목 하나를 받아 오는 전체 시간 한도와 홉당 한도(초)
_URL_FETCH_TOTAL_SEC = 60.0
_URL_FETCH_HOP_SEC = 20.0
# 관문의 응답 상한 초과 문구(`core.ssrf._read_capped*`) — 크기 초과를 SSRF 차단과 가른다
_OVERSIZE_PREFIX = "응답이 너무 큽니다"
# 관문 거부 사유 → 항목 실패 코드. 나머지 사유(userinfo·길이)는 `ssrf_blocked`다.
# `encoded_response`(압축 응답 — 풀기 전에 거부, #395)는 주소가 아니라 응답 형식 문제라
# `fetch_failed`다
_EGRESS_FAILURE_CODES = {"host_not_allowed": "host_not_allowed", "encoded_response": "fetch_failed"}


def _utcnow() -> datetime:
    """규칙 제목의 기준 시각 — 시험이 고정한다."""
    return datetime.now(UTC)


def mcp_external_ref(namespace: str, external_id: str) -> str:
    """`sources.external_ref`에 넣는 중복 키 — 다른 생산자 값과 겹치지 않게 접두사를 둔다."""
    return f"mcp:{namespace}:{external_id}"


def _parse_item(raw: Any) -> tuple[SaveSourceItem | None, str | None]:
    """항목 하나를 검증한다. 제목은 먼저 정리한다. 실패면 (None, 로그용 사유)."""
    if not isinstance(raw, dict):
        return None, f"항목이 객체가 아님({type(raw).__name__})"
    data = dict(raw)
    if isinstance(data.get("title"), str):
        data["title"] = clean_text(data["title"], _TITLE_MAX_CHARS)
    try:
        return SaveSourceItem.model_validate(data), None
    except ValidationError as e:
        # 입력값(본문 원문)은 싣지 않는다 — 위치와 오류 종류만 남긴다
        return None, "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '-'}:{err['type']}"
            for err in e.errors(include_input=False)
        )


def _reported_external_id(raw: Any) -> str | None:
    """실패 항목의 출력용 external_id — 형식에 맞을 때만 되돌려준다."""
    value = raw.get("external_id") if isinstance(raw, dict) else None
    if isinstance(value, str) and len(value) <= 200 and _EXTERNAL_ID_RE.fullmatch(value):
        return value
    return None


def _render_items(node: dict[str, Any], state: dict[str, Any]) -> tuple[list[Any], int]:
    node_id = node.get("id")
    rendered = render(node["items"], state)
    try:
        parsed = json.loads(rendered)
    except ValueError as e:
        raise AgentRuntimeError(f"save_sources 노드 [{node_id}]의 items가 JSON이 아닙니다") from e
    if not isinstance(parsed, list):
        raise AgentRuntimeError(f"save_sources 노드 [{node_id}]의 items는 JSON 배열이어야 합니다")
    return parsed[:MAX_INPUT_ITEMS], len(parsed)


def _card_host(item: SaveSourceItem) -> str | None:
    if item.url is None:
        return None
    try:
        host = urlsplit(item.url).hostname
    except ValueError:
        return None
    return host[:_CARD_HOST_CHARS] if host else None


def _approval_card(
    ctx: RunContext,
    node: dict[str, Any],
    parsed: list[tuple[SaveSourceItem | None, str | None]],
    total: int,
) -> dict[str, Any]:
    """승인 카드 — 항목 수와 검증을 통과한 앞 5개 항목의 제목·호스트, 대상만(리뷰 M5).

    본문·URL 경로·쿼리는 싣지 않는다. 제목은 100자, 호스트는 253자로 자르고, 카드 전체가
    `_CARD_MAX_CHARS`를 넘으면 목록을 뒤에서부터 줄인다.
    """
    head = [item for item, _ in parsed if item is not None][:_CARD_ITEMS]
    titles = [clean_text(item.title, _CARD_TITLE_CHARS) for item in head]
    hosts: list[str] = []
    for item in head:
        host = _card_host(item)
        if host and host not in hosts:
            hosts.append(host)
    rule = node.get("notebook_rule")
    card = {
        "namespace": node["namespace"],
        # 규칙이면 원문(해석한 노트북이 아니다 — 승인과 실행 사이에 달이 바뀔 수 있다)
        "target": rule if rule is not None else str(ctx.notebook_id),
        "count": len(parsed),
        "total": total,
        "titles": titles,
        "hosts": hosts,
    }
    while len(json.dumps(card, ensure_ascii=False)) > _CARD_MAX_CHARS and (titles or hosts):
        (hosts if len(hosts) >= len(titles) else titles).pop()
    return card


def _pattern_meet(a: str, b: str) -> str | None:
    """두 호스트 패턴이 함께 허용하는 호스트를 패턴 하나로. 겹치지 않으면 None."""
    a_wild, b_wild = a.startswith("*."), b.startswith("*.")
    if not b_wild:
        return b if host_matches(b, [a]) else None
    if not a_wild:
        return a if host_matches(a, [b]) else None
    # 둘 다 `*.x` — 더 깊은 쪽이 교집합이다(같거나 하위 도메인일 때)
    if host_matches(a[2:], [b[2:], b]):
        return a
    return b if host_matches(b[2:], [a]) else None


def _effective_hosts(node_hosts: Any, global_hosts: tuple[str, ...]) -> tuple[str, ...]:
    """노드 `allowed_hosts` ∩ 전역 `AGENT_HTTP_ALLOWED_HOSTS` — 전역이 비면 노드 목록 그대로다."""
    hosts = [h for h in node_hosts if isinstance(h, str)] if isinstance(node_hosts, list) else []
    if not global_hosts:
        return tuple(hosts)
    met: list[str] = []
    for a in hosts:
        for b in global_hosts:
            pattern = _pattern_meet(a, b)
            if pattern is not None and pattern not in met:
                met.append(pattern)
    return tuple(met)


def _url_host(url: str) -> str | None:
    """로그용 호스트 — URL 경로·쿼리는 남기지 않는다."""
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


class _PdfFetcher:
    """URL(PDF) 항목 받아 오기 — 노드 실행마다 처음 쓸 때 정책·상한을 한 번 정한다."""

    def __init__(self, policy: EgressPolicy | None, max_bytes: int, allow_private: bool) -> None:
        self.policy = policy  # None이면 허용 호스트가 없다 — 받지 않는다
        self.max_bytes = max_bytes
        self.allow_private = allow_private

    @classmethod
    async def create(cls, db: "AsyncSession", node: dict[str, Any]) -> "_PdfFetcher":
        from biznotes_core.services.agents.policy import agent_egress_policy
        from biznotes_core.services.ingestion.upload_limits import effective_max_upload_bytes

        settings = get_settings()
        base = agent_egress_policy()
        hosts = _effective_hosts(node.get("allowed_hosts"), base.allowed_hosts)
        policy = (
            EgressPolicy(
                fetch_only=True,
                allowed_hosts=hosts,
                max_url_chars=base.max_url_chars,
                max_query_chars=base.max_query_chars,
            )
            if hosts
            else None
        )
        max_bytes = await effective_max_upload_bytes(db, settings)
        return cls(policy, max_bytes, settings.ssrf_allow_private)

    async def fetch(self, url: str) -> tuple[bytes | None, str, str]:
        """(PDF 바이트, "", 로그) 또는 (None, 실패 코드, 로그 사유). 잠금·트랜잭션 밖에서 부른다."""
        from biznotes_core.services.ingestion.admission import AdmissionError, admit_bytes

        host = _url_host(url)
        if self.policy is None:
            return None, "host_not_allowed", f"허용 호스트 없음 host={host}"
        try:
            with anyio.fail_after(_URL_FETCH_TOTAL_SEC):
                resp = await ssrf_safe_request(
                    "GET",
                    url,
                    allow_private=self.allow_private,
                    timeout=_URL_FETCH_HOP_SEC,
                    egress=self.policy,
                    # 상한 + 1바이트까지 받는다 — 업로드처럼 판정은 `admit_bytes`가 한다
                    max_bytes=self.max_bytes + 1,
                )
        except EgressBlockedError as e:  # 관문이 이미 `agent_http_blocked`로 남겼다
            return None, _EGRESS_FAILURE_CODES.get(e.reason, "ssrf_blocked"), (
                f"egress={e.reason} host={host}"
            )
        except SSRFBlockedError as e:
            message = str(e)
            if message.startswith(_OVERSIZE_PREFIX):
                return None, "too_large", f"응답 상한 초과 host={host}"
            # 문구의 앞부분만 — 뒤에 붙는 호스트·해석 오류 상세는 남기지 않는다
            return None, "ssrf_blocked", f"{message.split(':', 1)[0]} host={host}"
        except TimeoutError:
            return None, "fetch_failed", f"시간 초과 host={host}"
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as e:
            return None, "fetch_failed", f"{type(e).__name__} host={host}"
        content_type = resp.headers.get("content-type")
        if not resp.is_success:
            return None, "fetch_failed", f"status={resp.status_code} host={host}"
        data = resp.content
        try:
            admit_bytes(data, _PDF_MIME, settings=get_settings(), max_bytes=self.max_bytes)
        except AdmissionError as e:
            code = "too_large" if e.code == "too_large" else "not_pdf"
            return None, code, f"admission={e.code} content_type={content_type} host={host}"
        return data, "", f"content_type={content_type} host={host}"


async def _can_write(db: "AsyncSession", ctx: RunContext, notebook_id: uuid.UUID) -> bool:
    """run 사용자가 지금 이 노트북에 쓸 수 있는가 — 항목마다 DB에서 다시 읽는다."""
    from biznotes_core.models import Notebook, User
    from biznotes_core.services.notebooks import get_permission

    user = await db.get(User, ctx.user_id, populate_existing=True)
    if user is None:
        raise AgentRuntimeError("save_sources 노드의 실행 사용자를 찾을 수 없습니다")
    notebook = await db.get(Notebook, notebook_id, populate_existing=True)
    if notebook is None:
        return False
    return await get_permission(db, notebook, user) in ("owner", "write")


class _Outcome:
    """항목 결과 모음 — 출력 dict를 만든다."""

    def __init__(self, ctx: RunContext, node_id: str) -> None:
        self.ctx = ctx
        self.node_id = node_id
        self.created: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.notebook_ids: list[str] = []

    def fail(self, index: int, external_id: str | None, code: str, reason: str | None) -> None:
        self.failed.append(
            {
                "index": index,
                "external_id": external_id,
                "code": code,
                "message": SAVE_SOURCE_FAILURE_MESSAGES[code],
            }
        )
        logger.info(
            "save_sources 항목 실패",
            run_id=str(self.ctx.run_id),
            node_id=self.node_id,
            index=index,
            code=code,
            reason=reason,
        )

    def output(self, total: int, seen_items: int, deferred: int) -> dict[str, Any]:
        """`seen_items`는 본 입력 항목 수(입력 상한 이내), `deferred`는 처리 상한으로 남긴 새 항목
        수다."""
        truncated = total > seen_items or deferred > 0
        text = (
            f"소스 {len(self.created)}개를 만들었습니다. 중복 {len(self.skipped)}개는 건너뛰었고 "
            f"{len(self.failed)}개는 실패했습니다."
        )
        if deferred:
            text += f" 새 항목 {deferred}개는 처리 상한을 넘어 다음 실행에서 처리합니다."
        if total > seen_items:
            text += f" 항목 {total}개 중 앞 {seen_items}개만 보았습니다."
        logger.info(
            "save_sources 완료",
            run_id=str(self.ctx.run_id),
            node_id=self.node_id,
            created=len(self.created),
            skipped=len(self.skipped),
            failed=len(self.failed),
            total=total,
            deferred=deferred,
        )
        return {
            "created": self.created,
            "skipped_duplicate": self.skipped,
            "failed": self.failed,
            "notebook_ids": self.notebook_ids,
            "truncated": truncated,
            "total": total,
            "text": text,
        }


async def _exec_save_sources(ctx: RunContext, node: dict, state: dict) -> dict[str, Any]:
    from biznotes_core.services.agents.policy import (
        direct_node_needs_gate,
        save_sources_config_errors,
    )
    from biznotes_core.services.agents.tool_registry import sanitize_for_approval_card

    node_id = str(node.get("id"))
    # 저장 헬퍼와 같은 검사 — 검증기 전에 저장된 정의도 쓰기 전에 실패한다
    errors = save_sources_config_errors(node)
    if errors:
        raise AgentRuntimeError(f"save_sources 노드 [{node_id}] 설정 오류: {'; '.join(errors)}")
    if ctx.user_id is None:
        raise AgentRuntimeError(
            f"save_sources 노드 [{node_id}]는 실행 사용자가 있는 run에서만 실행할 수 있습니다"
        )
    namespace: str = node["namespace"]
    items, total = _render_items(node, state)
    parsed = [_parse_item(raw) for raw in items]

    if await direct_node_needs_gate(ctx, node):
        _direct_node_gate(
            ctx,
            node,
            # 렌더링한 항목 + 리터럴 설정 원문 — 해석한 대상 노트북은 넣지 않는다
            request={
                "namespace": namespace,
                "items": items,
                "total": total,
                "allowed_hosts": node.get("allowed_hosts"),
                "notebook_rule": node.get("notebook_rule"),
            },
            tool_name=f"save_sources:{namespace}",
            arguments_summary=sanitize_for_approval_card(
                "save_sources", _approval_card(ctx, node, parsed, total)
            ),
        )

    # 항목 쓰기는 run 세션이 아닌 따로 연 세션에서 한다 — 항목마다 커밋하고 실패하면 되돌리는데,
    # 되돌리기는 그 세션의 ORM 객체를 모두 만료시킨다(run 세션의 객체를 건드리지 않는다)
    async with ctx.isolated_branch(mark_parallel=False) as (wctx, _isolated):
        return await _save_items(wctx.db, ctx, node, items, parsed, total)


async def _save_items(
    db: "AsyncSession",
    ctx: RunContext,
    node: dict[str, Any],
    items: list[Any],
    parsed: list[tuple[SaveSourceItem | None, str | None]],
    total: int,
) -> dict[str, Any]:
    from biznotes_core.models import User
    from biznotes_core.services.agents.errors import ApprovalRequired
    from biznotes_core.services.ingestion.source_create import (
        SourceAdmission,
        create_file_source,
        create_text_source,
        find_external_source,
        find_external_sources,
    )
    from biznotes_core.services.notebook_series import NotebookSeries

    node_id = str(node.get("id"))
    namespace: str = node["namespace"]
    assert ctx.user_id is not None  # 호출자가 확인했다
    series: NotebookSeries | None = None
    # 사전 중복 조회 범위 — 규칙이면 계열(이번 실행에 만든 노트북도 더한다), 아니면 run 노트북
    scope: list[uuid.UUID] = []
    if node.get("notebook_rule") is not None:
        if await db.get(User, ctx.user_id) is None:
            raise AgentRuntimeError("save_sources 노드의 실행 사용자를 찾을 수 없습니다")
        series = NotebookSeries.from_rule(
            node["notebook_rule"], owner_id=ctx.user_id, now=_utcnow()
        )
        scope = await series.member_ids(db)
    elif await _can_write(db, ctx, ctx.notebook_id):
        scope = [ctx.notebook_id]
    # 이미 있는 항목은 한 번에 찾아 처리 상한에서 뺀다(잠금 없는 조회 — 판정은 잠금 안에서 다시
    # 한다). 쓸 수 없는 노트북이면 찾지 않는다 — 항목마다 `forbidden`이 된다
    refs = [mcp_external_ref(namespace, item.external_id) for item, _ in parsed if item is not None]
    existing: dict[str, SourceAdmission] = await find_external_sources(db, scope, refs)
    limit = get_settings().agent_save_sources_max_items
    attempted = 0
    deferred = 0

    out = _Outcome(ctx, node_id)
    fetcher: _PdfFetcher | None = None  # 첫 url 항목에서 만든다
    # 같은 배치 안의 같은 키 — 처음 본 항목의 결과(없으면 None)
    seen: dict[str, dict[str, Any] | None] = {}
    for index, (item, reason) in enumerate(parsed):
        if item is None:
            out.fail(index, _reported_external_id(items[index]), "invalid_item", reason)
            continue
        ref = mcp_external_ref(namespace, item.external_id)
        if ref in seen:
            prior = seen[ref] or {}
            out.skipped.append(
                {
                    "index": index,
                    "external_id": item.external_id,
                    "source_id": prior.get("source_id"),
                    "notebook_id": prior.get("notebook_id"),
                }
            )
            continue
        seen[ref] = None
        if ref in existing:
            entry = _entry(index, item.external_id, existing[ref])
            seen[ref] = entry
            out.skipped.append(entry)
            continue
        if attempted >= limit:  # 처리 상한 — 다음 실행에서 이어진다
            deferred += 1
            continue
        attempted += 1

        _assert_lease_valid(ctx, node)  # 회수된 run에서 쓰지 않는다
        # 대상 — 규칙이면 항목 트랜잭션 안에서 계열이 정한다. 계열 노트북은 run 사용자 소유만
        # 고르고 새로 만들 때도 그 명의라 쓰기 권한(owner)이 탐색 조건에 들어 있다
        target = None if series is not None else ctx.notebook_id
        if target is not None and not await _can_write(db, ctx, target):
            out.fail(index, item.external_id, "forbidden", f"notebook={target}")
            continue
        place = series.placement(ref) if series is not None else None
        dup_scope = scope if series is not None else [ctx.notebook_id]
        try:
            if item.content is not None:
                result = await create_text_source(
                    db,
                    notebook_id=target,
                    place=place,
                    title=item.title,
                    text=item.content,
                    mime_type=item.mime_type,
                    external_ref=ref,
                    duplicate_scope=dup_scope,
                )
            else:
                assert item.url is not None
                found = await find_external_source(db, dup_scope, ref)
                if found is not None:  # 사전 조회 뒤에 다른 실행이 만들었다 — 받아 오지 않는다
                    result = found
                else:
                    fetcher = fetcher or await _PdfFetcher.create(db, node)
                    # 받는 동안 트랜잭션·연결을 쥐지 않는다 — 사전 조회의 읽기 트랜잭션을 닫는다
                    await _release_connections(ctx, db)
                    data, code, detail = await fetcher.fetch(item.url)
                    if data is None:
                        out.fail(index, item.external_id, code, detail)
                        continue
                    _assert_lease_valid(ctx, node)  # 받는 동안 lease를 잃었으면 쓰지 않는다
                    result = await create_file_source(
                        db,
                        notebook_id=target,
                        place=place,
                        title=item.title,
                        data=data,
                        mime_type=_PDF_MIME,
                        suffix=".pdf",
                        origin_url=item.url,
                        external_ref=ref,
                        duplicate_scope=dup_scope,
                    )
        except (AgentRuntimeError, ApprovalRequired):
            raise  # lease 상실·승인·정책 — 노드를 멈춘다(취소는 BaseException이라 잡히지 않는다)
        except Exception as e:
            await _recover_session(db, ctx, e)
            if item.url is not None:
                # URL 항목은 예외 유형만 남긴다 — DB 예외 문자열·추적의 바인드 값에 `origin_url`
                # (쿼리의 비밀값 포함)이 실린다
                logger.warning(
                    "save_sources 항목 저장 실패",
                    run_id=str(ctx.run_id),
                    node_id=node_id,
                    index=index,
                    error=type(e).__name__,
                )
            else:
                logger.warning(
                    "save_sources 항목 저장 실패",
                    run_id=str(ctx.run_id),
                    node_id=node_id,
                    index=index,
                    exc_info=True,
                )
            out.fail(index, item.external_id, "save_failed", type(e).__name__)
            continue
        if series is not None and result.notebook_id not in scope:
            scope.append(result.notebook_id)
        if result.outcome == "full":
            out.fail(index, item.external_id, "notebook_full", f"notebook={result.notebook_id}")
            continue
        entry = _entry(index, item.external_id, result)
        seen[ref] = entry
        if result.outcome == "created":
            out.created.append(entry)
            if entry["notebook_id"] not in out.notebook_ids:
                out.notebook_ids.append(entry["notebook_id"])
        else:
            out.skipped.append(entry)
    return out.output(total, len(items), deferred)


def _entry(index: int, external_id: str, result: "SourceAdmission") -> dict[str, Any]:
    return {
        "index": index,
        "external_id": external_id,
        "source_id": str(result.source_id) if result.source_id is not None else None,
        "notebook_id": str(result.notebook_id),
    }


async def _recover_session(db: "AsyncSession", ctx: RunContext, error: Exception) -> None:
    """DB 오류 뒤 쓰기 세션을 다음 항목이 쓸 수 있게 되돌린다 — 앞 항목들은 이미 커밋됐다."""
    from sqlalchemy.exc import SQLAlchemyError

    if not isinstance(error, SQLAlchemyError):
        return  # 원본 업로드 실패 등 — 세션은 멀쩡하다
    try:
        await db.rollback()
    except Exception as e:  # noqa: BLE001 — 되돌리지 못하면 다음 항목이 같은 오류로 실패한다
        # 유형만 — 추적에는 원래 예외(바인드 값 포함)가 이어 붙는다
        logger.warning(
            "save_sources 세션 되돌리기 실패", run_id=str(ctx.run_id), error=type(e).__name__
        )


async def _release_connections(ctx: RunContext, db: "AsyncSession") -> None:
    """받아 오기 전에 읽기 트랜잭션을 닫아 연결을 풀에 돌려준다(#288 선례 — 빈 UoW로 커밋).

    쓰기 세션(`db`)은 이 노드가 연 세션이고 이 시점에 쓰기가 없다. run 세션이 따로 있으면(격리된
    쓰기 세션) 커밋하지 않은 변경이 없을 때만 닫는다 — 남의 쓰기를 대신 커밋하지 않는다.
    """
    from biznotes_core.core.uow import unit_of_work

    if db.in_transaction():
        async with unit_of_work(db):
            pass
    run_db = ctx.db
    if run_db is db or not run_db.in_transaction():
        return
    if run_db.new or run_db.dirty or run_db.deleted:
        logger.info("save_sources run 세션에 미커밋 변경이 있어 연결을 둔 채 받는다")
        return
    async with unit_of_work(run_db):
        pass


__all__ = ["SAVE_SOURCE_FAILURE_MESSAGES", "mcp_external_ref"]
