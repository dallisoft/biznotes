"""스킬 version 파일 읽기 — 점진적 공개의 2·3단계 저장소 경계 (표준 계획 D6).

"PLAN"은 sl-agents `docs/plans/agent-skills-mcp-standards/PLAN.md` §3(AC4)이다. 코어에서는
`docs/plans/agent-skills-standard/plan.md` D6이 이 모듈을 `runtime.py`의 두 도구에 연결한다.

1. **카탈로그**: DB의 `skill_versions` 행만 읽는다. 저장소 object는 읽지 않는다(`runtime.py`).
2. **활성화**: `SKILL.md` **하나만** 읽는다.
3. **읽기**: 요청한 파일 **하나만** 읽는다.

읽기는 활성화 때 고정한 version의 `file_tree`에 있는 경로만 허용한다(다른 version·트리 밖 경로
거부). 크기는 `file_tree`의 기록으로 **읽기 전에** 먼저 거르고, 읽은 뒤 실제 크기로 다시 본다.
모든 거부는 reason 코드와 structlog 이벤트를 남기고 `SkillFileAccessError`를 던진다.
"""

import uuid
from dataclasses import dataclass
from typing import Any

import anyio
import structlog

from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills.validator import SkillValidationError, validate_path

logger = structlog.get_logger()


class SkillFileAccessError(ValueError):
    """스킬 파일 접근 거부. reason 코드로 분류한다(관측성·테스트용)."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class FileEntry:
    """`file_tree` 한 항목 — 읽기 전에 쓸 수 있는 사실."""

    path: str
    size: int | None
    media_type: str


def file_entries(version: Any) -> dict[str, FileEntry]:
    """version 행의 `file_tree` → 경로별 항목. 저장소를 읽지 않는다."""
    entries: dict[str, FileEntry] = {}
    for item in getattr(version, "file_tree", None) or []:
        path = str(item.get("path") or "")
        if not path:
            continue
        size = item.get("size")
        entries[path] = FileEntry(
            path=path,
            size=size if isinstance(size, int) else None,
            media_type=str(item.get("media_type") or "application/octet-stream"),
        )
    return entries


def _reject(version_id: uuid.UUID, path: str, reason: str, message: str) -> SkillFileAccessError:
    logger.warning("skill.file.rejected", version_id=str(version_id), path=path, reason=reason)
    return SkillFileAccessError(reason, message)


async def read_version_file(
    version: Any,
    path: str,
    *,
    max_bytes: int,
    budget_used: int,
    budget_limit: int,
) -> tuple[FileEntry, bytes]:
    """고정한 version에서 파일 하나를 읽는다. 범위·크기·누적 한도를 모두 검사한다.

    `budget_used`는 이 run이 이미 읽은 바이트다. 호출자가 결과 크기를 누적 셀에 더한다 —
    바이너리처럼 모델에 넣지 않는 결과는 더하지 않을 수 있게 하기 위해서다.
    """
    try:
        rel = validate_path(path)
    except SkillValidationError as e:
        raise _reject(version.id, path, e.reason, str(e)) from e

    entry = file_entries(version).get(rel)
    if entry is None:
        raise _reject(version.id, rel, "out_of_scope", f"이 스킬 버전에 없는 파일입니다: {rel}")
    if entry.size is not None:
        # 기록된 크기로 먼저 거른다 — 한도를 넘는 파일은 저장소에서 읽지도 않는다
        _check_size(version.id, rel, entry.size, max_bytes, budget_used, budget_limit)

    try:
        data = await anyio.to_thread.run_sync(pkg.get_version_file, version.id, rel)
    except SkillValidationError as e:
        raise _reject(version.id, rel, e.reason, str(e)) from e
    except Exception as e:
        logger.warning(
            "skill.file.storage_error", version_id=str(version.id), path=rel, error=str(e)
        )
        raise SkillFileAccessError("not_found", f"스킬 파일을 읽을 수 없습니다: {rel}") from e
    # 실제 크기로 다시 본다 — `file_tree`의 기록을 믿지 않는다
    _check_size(version.id, rel, len(data), max_bytes, budget_used, budget_limit)
    logger.info("skill.file.read", version_id=str(version.id), path=rel, size=len(data))
    return entry, data


def _check_size(
    version_id: uuid.UUID,
    path: str,
    size: int,
    max_bytes: int,
    budget_used: int,
    budget_limit: int,
) -> None:
    if size > max_bytes:
        raise _reject(
            version_id,
            path,
            "file_too_large",
            f"한 번에 읽기에 너무 큰 파일입니다: {path} ({size}B > {max_bytes}B)",
        )
    if budget_used + size > budget_limit:
        raise _reject(
            version_id,
            path,
            "run_budget_exceeded",
            f"이 실행에서 읽을 수 있는 총량을 넘었습니다(누적 {budget_used}B + 요청 {size}B > "
            f"{budget_limit}B)",
        )
