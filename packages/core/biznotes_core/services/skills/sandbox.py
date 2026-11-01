"""번들 스크립트 실행 L2 — `run_skill_script` 도구와 샌드박스 발행 (표준 계획 D7, Phase 4).

에이전트 워커 쪽이다. 스크립트는 여기서 실행하지 않는다 — API·에이전트 워커에서 `subprocess`로
실행하지 않는다는 불변 조건(sl-HCI-4)을 이 모듈이 지킨다. 실행은 전용 샌드박스 워커가 한다
(`biznotes_core.workers.skill_sandbox`). 이 모듈은 이름으로만 발행한다
(`task_names.RUN_SKILL_SCRIPT`).

실행 조건(D7 "불변 조건") — **도구 노출**과 **호출** 두 곳에서 모두 확인한다.

1. `skill_script_backend`가 `disabled`가 아니다.
2. AgentRun 직접 실행이다(`ctx.skill_pins`가 있고 깊이 0). 채팅 진입 run도 포함한다(#86) —
   이 도구는 always-gated라(조건 4) 채팅 진입 run도 항상 사람 승인으로 멈춘다. 채팅은 자동 승인
   정책도 쓰지 않는다.
3. 그 version에 승인된 `scripts` 검토 요청이 있다(D8). 승인 철회는 다음 호출부터 바로 듣는다.
4. 승인 게이트를 통과했다 — `run_skill_script`는 always-gated다(`tool_registry`).

결과(표준 출력·오류)는 **데이터** 등급이다. 스킬 지침 태그는 이스케이프한다(위협 모델 T2).
"""

import html
import mimetypes
import os
import re
import time
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

import anyio
import structlog

from biznotes_core.config import get_settings
from biznotes_core.models import Skill, SkillVersion
from biznotes_core.models.skill import SKILL_RUNTIME_STANDARD
from biznotes_core.services.agents.errors import ToolUnavailableError
from biznotes_core.services.agents.react.skill_guard import escape_skill_tags
from biznotes_core.services.skills import files as skill_files
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills.exposure import SCRIPTS_KIND, has_approval
from biznotes_core.services.skills.standard import SCRIPT_EXTENSIONS as SCRIPT_EXTENSIONS
from biznotes_core.services.skills.standard import is_runnable_script
from biznotes_core.workers import task_names

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from biznotes_core.services.agents.context import RunContext

logger = structlog.get_logger()

RUN_SKILL_SCRIPT_TOOL = "run_skill_script"
# Python 3.12 표준 `mimetypes`에는 OOXML 형식이 없고(3.14에서 추가) slim 이미지에는
# `/etc/mime.types`도 없다 — 샌드박스가 만드는 문서 형식은 직접 정한다(#127).
_OUTPUT_MEDIA_TYPES: dict[str, str] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_ARGS = 32
MAX_ARG_CHARS = 4096
MAX_INPUTS = 10  # inputs + artifact_inputs 합계
# 입력 합계 상한 — 샌드박스 워커(`workers/skill_sandbox/job.py`)의 MAX_INPUT_BYTES와 같다.
# 워커가 거부하기 전에 디스패치에서 먼저 거른다(#133)
MAX_INPUT_BYTES = 64 << 20
# 워커의 입력 이름 규칙(`job._INPUT_NAME`, 점으로 시작 금지)과 같다
_INPUT_NAME = re.compile(r"^[^/\x00-\x1f\x7f]{1,128}$")
# 발행·전송에 드는 여유. presigned URL 수명은 대기 한도 + 이 값이다
_TRANSFER_MARGIN_SEC = 60
_BLPOP_SOCKET_MARGIN_SEC = 10
# 메시지 수명에서 빼는 내려받기·올리기 여유. 설정 검증이 대기 한도 > 실행 한도 + 이 값을 강제한다
EXPIRES_TRANSFER_MARGIN_SEC = 30


class SkillScriptUnavailable(ToolUnavailableError):
    """스크립트를 실행할 수 없다 — 도구 오류로 모델에게 돌아가고 run은 계속된다."""


# ---------- 백엔드 ----------


class SkillSandbox(Protocol):
    async def run(
        self, job: dict[str, Any], *, wait_sec: float, expires_sec: float
    ) -> dict[str, Any]:
        """작업을 보내고 회신을 기다린다. 대기 한도를 넘으면 `SkillScriptUnavailable`이다."""
        ...


class DisabledSandbox:
    async def run(
        self, job: dict[str, Any], *, wait_sec: float, expires_sec: float
    ) -> dict[str, Any]:
        raise SkillScriptUnavailable("스크립트 실행이 꺼져 있습니다")


class ProcessSandbox:
    """전용 샌드박스 워커 큐로 보내고 회신 키를 BLPOP한다(D7 "결과 수신").

    태스크 안에서 `AsyncResult.get()`을 쓰지 않는다. 대기 한도를 넘기면 `revoke(terminate=True)`를
    보내 아직 시작하지 않은 메시지는 버리게 하고, 실행 중이면 워커가 스크립트를 정리하게 한다.
    """

    async def run(
        self, job: dict[str, Any], *, wait_sec: float, expires_sec: float
    ) -> dict[str, Any]:
        import json

        from redis.asyncio import Redis
        from redis.exceptions import RedisError

        from biznotes_core.workers.celery_app import celery_app

        call_id = str(job["call_id"])

        def _send() -> None:
            celery_app.send_task(
                task_names.RUN_SKILL_SCRIPT,
                args=[job],
                task_id=call_id,
                queue=task_names.SKILL_SANDBOX_QUEUE,
                expires=expires_sec,
            )

        await anyio.to_thread.run_sync(_send)
        # 소켓 읽기 한도가 BLPOP 대기보다 짧으면 회신 전에 TimeoutError가 난다(스택 스모크 실측)
        redis = Redis.from_url(
            get_settings().redis_url, socket_timeout=wait_sec + _BLPOP_SOCKET_MARGIN_SEC
        )
        try:
            popped = await redis.blpop(
                [task_names.SKILL_SANDBOX_REPLY_PREFIX + call_id], timeout=wait_sec
            )
        except RedisError as e:
            # 회신을 못 받은 것과 같다 — 아래에서 revoke하고 도구 오류로 끝낸다
            logger.warning("skill.script.reply_error", call_id=call_id, error=type(e).__name__)
            popped = None
        finally:
            await redis.aclose()
        if popped is None:

            def _revoke() -> None:
                celery_app.control.revoke(call_id, terminate=True)

            await anyio.to_thread.run_sync(_revoke)
            logger.warning("skill.script.timeout", call_id=call_id, wait_sec=wait_sec)
            raise SkillScriptUnavailable(
                f"스크립트 실행이 대기 한도({int(wait_sec)}초) 안에 끝나지 않았습니다"
            )
        body = json.loads(popped[1])
        if not isinstance(body, dict):
            raise SkillScriptUnavailable("샌드박스 회신 형식이 올바르지 않습니다")
        return body


def get_sandbox() -> SkillSandbox:
    backend = get_settings().skill_script_backend
    if backend == "process":
        return ProcessSandbox()
    if backend == "container":
        raise SkillScriptUnavailable("container 백엔드는 아직 구현되지 않았습니다")
    return DisabledSandbox()


def backend_enabled() -> bool:
    return get_settings().skill_script_backend != "disabled"


# ---------- 자격 ----------


def script_paths(version: Any) -> list[str]:
    """version의 실행 가능한 스크립트 — `scripts/` 아래이고 확장자 허용 목록에 있는 파일."""
    return sorted(path for path in skill_files.file_entries(version) if is_runnable_script(path))


async def scripts_approved(db: "AsyncSession", version_id: uuid.UUID) -> bool:
    """그 version에 승인된 `scripts` 검토 요청이 있는가(D8). 철회·거절은 승인이 아니다."""
    return await has_approval(db, version_id, (SCRIPTS_KIND,))


def _direct_run(ctx: "RunContext") -> bool:
    """최상위 AgentRun의 스킬 직접 실행인가. 채팅 진입(`trigger="chat"`) run도 포함한다(#86).

    깊이 0의 pin은 워커가 AgentRun을 실행하는 `execute_skill_package`에서만 생긴다(재개 때는
    그 checkpoint에서 복원된다, 호출자 계약은 `builder.execute_skill_package`). 그래서 승인
    대기를 영속화하고 재개할 수 있는 run이다. 채팅 카탈로그 경로에는 pin이 없고, 이 도구는
    `CHAT_SAFE_TOOLS`에도 없다.

    서브에이전트(`call_agent`·`sub_workflow`)로 부른 표준 스킬도 자식 컨텍스트에 pin을 만든다 —
    깊이 0만 직접 실행으로 본다(리뷰 R4P-03). `child()`는 pin을 물려주지 않는다.
    """
    return bool(ctx.skill_pins) and getattr(ctx, "depth", 0) == 0


async def _runnable(ctx: "RunContext", name: str, version_id: str) -> tuple[Any, list[str]] | None:
    version = await ctx.db.get(SkillVersion, uuid.UUID(version_id))
    if version is None or version.runtime_profile != SKILL_RUNTIME_STANDARD:
        return None
    skill = await ctx.db.get(Skill, version.skill_id)
    if skill is None or not skill.enabled:
        return None
    if not await scripts_approved(ctx.db, version.id):
        return None
    paths = script_paths(version)
    return (version, paths) if paths else None


async def run_skill_script_spec(ctx: "RunContext") -> dict[str, Any] | None:
    """도구 스펙. 조건 1~3을 만족하는 고정 스킬이 없으면 None — 도구를 등록하지 않는다.

    수락 기준 32.
    """
    if not backend_enabled() or not _direct_run(ctx):
        return None
    names: list[str] = []
    scripts: set[str] = set()
    for name, version_id in sorted(ctx.skill_pins.items()):
        found = await _runnable(ctx, name, version_id)
        if found is None:
            continue
        names.append(name)
        scripts.update(found[1])
    if not names:
        return None
    return {
        "type": "function",
        "function": {
            "name": RUN_SKILL_SCRIPT_TOOL,
            "description": (
                "활성화한 스킬의 번들 스크립트를 격리된 환경에서 실행한다. "
                "네트워크가 없고 런타임을 설치할 수 없다. "
                "스크립트가 outputs/ 디렉터리에 쓴 파일은 이 실행의 산출물이 된다. "
                "args는 명령행 인자 목록이다(셸을 거치지 않는다). inputs에는 이 실행의 이전 "
                "산출물 이름을, artifact_inputs에는 이 노트북의 다른 실행이 만든 산출물"
                "(artifact_list의 run_id·name)을 줄 수 있다 — 스크립트는 둘 다 "
                "../inputs/<이름>으로 읽는다(이름이 겹치면 안 된다). "
                "실행 전에 사람의 승인이 필요하다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": names, "description": "스킬 name"},
                    "script": {
                        "type": "string",
                        "enum": sorted(scripts),
                        "description": "스킬 루트 기준 스크립트 경로",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "명령행 인자",
                    },
                    "inputs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "입력으로 넘길 이 실행의 산출물 이름(선택)",
                    },
                    "artifact_inputs": _ARTIFACT_INPUTS_SCHEMA,
                },
                "required": ["name", "script"],
            },
        },
    }


# ---------- 호출 ----------


def _str_list(value: Any, *, max_items: int, max_chars: int, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise SkillScriptUnavailable(f"{what}는 문자열 {max_items}개 이하의 목록이어야 합니다")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > max_chars or "\x00" in item:
            raise SkillScriptUnavailable(f"{what} 항목은 {max_chars}자 이하의 문자열이어야 합니다")
        out.append(item)
    return out


def _signer_endpoint() -> str:
    """샌드박스가 저장소에 닿는 주소 — local은 api 기준 URL, s3는 서명할 `host:port`."""
    s = get_settings()
    if s.skill_script_storage_endpoint:
        return s.skill_script_storage_endpoint
    return s.s3_endpoint if s.storage_backend == "s3" else s.api_base_url


def _presign(
    method: str, bucket: str, key: str, expires: timedelta, *, max_bytes: int | None = None
) -> str:
    from biznotes_core.services import storage

    return storage.presigned_url_for(
        method,
        bucket,
        key,
        expires=expires,
        endpoint=_signer_endpoint(),
        region=get_settings().skill_script_storage_region,
        max_bytes=max_bytes,
    )


def _package_files(version: Any, expires: timedelta) -> list[dict[str, Any]]:
    bucket = get_settings().storage_bucket_skill_packages
    out: list[dict[str, Any]] = []
    for item in getattr(version, "file_tree", None) or []:
        path = str(item.get("path") or "")
        digest = item.get("hash")
        size = item.get("size")
        if not path or not isinstance(digest, str) or not isinstance(size, int):
            # 기록이 없으면 샌드박스가 무결성을 확인할 수 없다 — 보내지 않는다
            raise SkillScriptUnavailable(f"스킬 파일 기록이 불완전합니다: {path or '(경로 없음)'}")
        key = pkg.version_prefix(version.id) + os.path.normpath(path)
        out.append(
            {
                "path": path,
                "url": _presign("GET", bucket, key, expires),
                "size": size,
                "sha256": digest,
            }
        )
    return out


_ARTIFACT_INPUTS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "산출물을 만든 실행 ID"},
            "name": {"type": "string", "description": "산출물 이름"},
            "path": {
                "type": "string",
                "description": "스크립트가 읽을 이름(선택, 기본 name) — 겹치거나 /가 있을 때",
            },
        },
        "required": ["run_id", "name"],
    },
    "description": "입력으로 넘길 이 노트북의 다른 실행 산출물(artifact_list 결과, 선택)",
}


def _artifact_refs(value: Any) -> list[tuple[uuid.UUID, str, str | None]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_INPUTS:
        raise SkillScriptUnavailable(
            f"artifact_inputs는 {{run_id, name}} {MAX_INPUTS}개 이하의 목록이어야 합니다"
        )
    out: list[tuple[uuid.UUID, str, str | None]] = []
    for item in value:
        if not isinstance(item, dict):
            raise SkillScriptUnavailable("artifact_inputs 항목은 {run_id, name}이어야 합니다")
        name, path = item.get("name"), item.get("path")
        try:
            run_id = uuid.UUID(str(item.get("run_id")))
        except (TypeError, ValueError):
            run_id = None
        if (
            run_id is None
            or not isinstance(name, str)
            or not name
            or len(name) > 1024
            or "\x00" in name
            or not (path is None or isinstance(path, str))
        ):
            raise SkillScriptUnavailable("artifact_inputs 항목은 {run_id, name}이어야 합니다")
        out.append((run_id, name, path or None))
    return list(dict.fromkeys(out))


async def _resolve_inputs(
    ctx: "RunContext", names: list[str], refs: list[tuple[uuid.UUID, str, str | None]]
) -> list[tuple[str, str]]:
    """입력 이름·참조를 (스크립트 경로, 저장소 키)로 확정한다 — DB 조회는 여기서 끝낸다.

    `inputs`는 이 run의 산출물만 찾고, 이름이 같으면 마지막 것이다(기존 동작). `artifact_inputs`는
    노트북 산출물 경계(#133, 채팅 run은 채팅에서만·노트북 권한 재확인) 안에서 찾고, 이름이 같으면
    첫 것이다(제품 다운로드와 같은 규칙). 승인 뒤 재개될 때도 실행 시점에 다시 확정한다.
    """
    from biznotes_core.services.agents.nodes.artifact_tools import find_artifact

    by_name = {a.get("name"): a for a in ctx.artifact_keys if isinstance(a, dict)}
    resolved: list[tuple[str, str]] = []
    for name in dict.fromkeys(names):
        entry = by_name.get(name)
        if entry is None:
            raise SkillScriptUnavailable(f"이 실행의 산출물이 아닙니다: {name}")
        resolved.append((name, entry["key"]))
    for run_id, name, path in refs:
        art = await find_artifact(ctx, run_id, name)
        if art is None:
            raise SkillScriptUnavailable(f"이 실행에서 볼 수 없는 산출물입니다: {run_id}/{name}")
        resolved.append((path or name, art.key))
    if len(resolved) > MAX_INPUTS:
        raise SkillScriptUnavailable(
            f"입력은 inputs와 artifact_inputs를 합쳐 {MAX_INPUTS}개까지입니다"
        )
    seen: set[str] = set()
    for path, _key in resolved:
        if path in seen:
            raise SkillScriptUnavailable(f"입력 이름이 겹칩니다(../inputs/{path}): {path}")
        if path.startswith(".") or not _INPUT_NAME.match(path):
            raise SkillScriptUnavailable(f"입력으로 넘길 수 없는 이름입니다: {path}")
        seen.add(path)
    return resolved


def _input_files(entries: list[tuple[str, str]], expires: timedelta) -> list[dict[str, Any]]:
    """확정한 입력의 presigned URL·크기·해시. 합계가 워커 상한을 넘으면 내려받기 전에 거부한다."""
    import hashlib

    from biznotes_core.services import storage

    bucket = get_settings().storage_bucket_artifacts
    total = sum(storage.stat_object_size(bucket, key) for _path, key in entries)
    if total > MAX_INPUT_BYTES:
        raise SkillScriptUnavailable(
            f"입력 합계가 {MAX_INPUT_BYTES >> 20}MiB를 넘습니다({total:,}B)"
        )
    out: list[dict[str, Any]] = []
    for path, key in entries:
        data = storage.get_object(bucket, key)
        out.append(
            {
                "path": path,
                "url": _presign("GET", bucket, key, expires),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return out


def _result_text(
    name: str, script: str, body: dict[str, Any], outputs: list[dict[str, str]]
) -> str:
    limit = get_settings().skill_script_output_chars
    stdout = str(body.get("stdout") or "")
    stderr = str(body.get("stderr") or "")
    # 모델에게는 합쳐서 상한까지 — stdout을 먼저 싣는다
    stdout_part = stdout[:limit]
    stderr_part = stderr[: max(0, limit - len(stdout_part))]
    truncated = len(stdout_part) < len(stdout) or len(stderr_part) < len(stderr)
    attrs = (
        f'skill="{html.escape(name)}" script="{html.escape(script)}" '
        f'exit_code="{body.get("exit_code")}" '
        f'timed_out="{str(bool(body.get("timed_out"))).lower()}"'
    )
    lines = [f"<script_result {attrs}>", "<stdout>", escape_skill_tags(stdout_part), "</stdout>"]
    if stderr_part:
        lines += ["<stderr>", escape_skill_tags(stderr_part), "</stderr>"]
    if truncated:
        lines.append(f"(출력이 {limit}자를 넘어 잘렸다)")
    if body.get("output_overflow"):
        lines.append("(스크립트가 출력 상한을 넘어 종료되었다)")
    if body.get("launcher_failed"):
        lines.append("(격리 환경을 준비하지 못해 스크립트를 실행하지 않았다)")
    if outputs:
        lines.append("<outputs>")
        lines += [f"<file>{html.escape(o['name'])}</file>" for o in outputs]
        lines.append("</outputs>")
    skipped = body.get("skipped") or []
    if skipped:
        lines.append(
            "수집하지 않은 outputs 항목: "
            + ", ".join(
                f"{html.escape(str(s.get('name')))}({html.escape(str(s.get('reason')))})"
                for s in skipped
                if isinstance(s, dict)
            )
        )
    lines.append("</script_result>")
    return "\n".join(lines)


def _register_outputs(
    ctx: "RunContext", slot_keys: list[str], body: dict[str, Any]
) -> list[dict[str, str]]:
    """회신한 산출물을 run 산출물로 등록한다. 저장소의 실제 크기로 다시 확인한다."""
    from biznotes_core.services import storage

    bucket = get_settings().storage_bucket_artifacts
    registered: list[dict[str, str]] = []
    seen: set[int] = set()
    for item in body.get("outputs") or []:
        if not isinstance(item, dict):
            continue
        slot = item.get("slot")
        name = item.get("name")
        if (
            not isinstance(slot, int)
            or isinstance(slot, bool)
            or not 0 <= slot < len(slot_keys)
            or slot in seen
            or not isinstance(name, str)
            or not skill_files_safe_name(name)
        ):
            continue
        seen.add(slot)
        key = slot_keys[slot]
        try:
            size = storage.stat_object_size(bucket, key)
        except Exception:  # noqa: BLE001 — 올라오지 않은 슬롯은 등록하지 않는다
            continue
        if size != item.get("size"):
            continue
        entry = {"key": key, "name": name, "content_type": _output_media_type(name)}
        ctx.artifact_keys.append(entry)
        registered.append(entry)
    return registered


def _output_media_type(name: str) -> str:
    known = _OUTPUT_MEDIA_TYPES.get(os.path.splitext(name)[1].lower())
    return known or mimetypes.guess_type(name)[0] or "application/octet-stream"


def skill_files_safe_name(name: str) -> bool:
    return (
        0 < len(name) <= 128
        and not name.startswith(".")
        and "/" not in name
        and "\\" not in name
        and all(ch.isprintable() for ch in name)
    )


async def dispatch_run_skill_script(ctx: "RunContext", _tool: str, args: dict[str, Any]) -> str:
    """스크립트 한 번 실행. 승인 게이트는 이 함수 **앞에서** 이미 통과했다(always-gated)."""
    settings = get_settings()
    if not backend_enabled():
        raise SkillScriptUnavailable("스크립트 실행이 꺼져 있습니다")
    if not _direct_run(ctx):
        raise SkillScriptUnavailable("스크립트는 스킬 직접 실행에서만 쓸 수 있습니다")
    name = str(args.get("name") or "").strip()
    script = str(args.get("script") or "")
    version_id = ctx.skill_activations.get(name)
    if version_id is None or ctx.skill_pins.get(name) != version_id:
        raise SkillScriptUnavailable(f"이 실행에서 활성화한 스킬이 아닙니다: {name}")
    found = await _runnable(ctx, name, version_id)
    if found is None:
        raise SkillScriptUnavailable(f"스크립트 실행이 승인되지 않은 스킬입니다: {name}")
    version, paths = found
    if script not in paths:
        raise SkillScriptUnavailable(f"이 스킬에서 실행할 수 없는 스크립트입니다: {script}")
    argv = _str_list(args.get("args"), max_items=MAX_ARGS, max_chars=MAX_ARG_CHARS, what="args")
    input_names = _str_list(args.get("inputs"), max_items=MAX_INPUTS, max_chars=128, what="inputs")
    inputs = await _resolve_inputs(ctx, input_names, _artifact_refs(args.get("artifact_inputs")))

    call_id = str(uuid.uuid4())
    wait_sec = float(settings.skill_script_wait_sec)
    timeout = settings.skill_script_timeout_sec
    # 메시지 수명 = 대기 한도 − 실행 한도 − 전송 여유: 늦게 집힌 메시지는 실행해도 회신을 받을 수
    # 없다. 내려받기·올리기 시간을 빼지 않으면 올리는 도중 revoke된다(리뷰 R4P-11)
    expires_sec = wait_sec - timeout - EXPIRES_TRANSFER_MARGIN_SEC
    url_ttl = timedelta(seconds=wait_sec + _TRANSFER_MARGIN_SEC)
    slot_keys = [
        f"{ctx.run_id}/scripts/{call_id}/{i}" for i in range(settings.skill_script_max_outputs)
    ]

    def _build() -> dict[str, Any]:
        from biznotes_core.services.storage import ensure_buckets

        ensure_buckets()
        bucket = settings.storage_bucket_artifacts
        slot_max = settings.skill_script_outputs_max_bytes
        return {
            "call_id": call_id,
            "start_by": time.time() + expires_sec,
            "script": script,
            "args": argv,
            "files": _package_files(version, url_ttl),
            "inputs": _input_files(inputs, url_ttl),
            # 슬롯 하나의 상한은 출력 합계 상한이다(합계는 샌드박스가 다시 자른다)
            "output_slots": [
                _presign("PUT", bucket, k, url_ttl, max_bytes=slot_max) for k in slot_keys
            ],
            "limits": {
                "wall_sec": timeout,
                "cpu_sec": timeout,
                "as_bytes": settings.skill_script_memory_bytes,
                "fsize_bytes": settings.skill_script_file_size_bytes,
                "outputs_max_bytes": settings.skill_script_outputs_max_bytes,
            },
        }

    job = await anyio.to_thread.run_sync(_build)
    logger.info(
        "skill.script.dispatch",
        call_id=call_id,
        skill_version_id=str(version.id),
        script=script,
        argc=len(argv),
        inputs=len(inputs),
    )
    body = await get_sandbox().run(job, wait_sec=wait_sec, expires_sec=expires_sec)
    status = body.get("status")
    evidence = {
        "kind": "skill_script",
        "name": name,
        "skill_version_id": str(version.id),
        "script": script,
        "call_id": call_id,
        "status": status,
        "exit_code": body.get("exit_code"),
    }
    ctx.tool_evidence.append(evidence)
    if status != "completed":
        reason = html.escape(str(body.get("reason") or status))
        logger.warning("skill.script.not_completed", call_id=call_id, status=status, reason=reason)
        raise SkillScriptUnavailable(f"스크립트를 실행하지 못했습니다: {reason}")
    registered = await anyio.to_thread.run_sync(_register_outputs, ctx, slot_keys, body)
    return _result_text(name, script, body, registered)
