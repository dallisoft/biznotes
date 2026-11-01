"""스킬 패키지 버킷의 고아 오브젝트 찾기·정리 (#151).

#151 이전의 스킬 삭제는 draft prefix만 지워 `versions/<version_id>/`가 남았다. 지금도 삭제 직후
오브젝트 정리가 실패하거나, freeze·가져오기가 오브젝트를 쓴 뒤 트랜잭션이 롤백되면 같은
고아가 생긴다. 이 모듈은 DB에 행이 없는 `drafts/<skill_id>/`·`versions/<version_id>/`를 찾는다.

사용법(기본은 미리보기 — 아무것도 지우지 않는다)::

    python -m biznotes_core.services.skills.orphans
    python -m biznotes_core.services.skills.orphans --apply
    python -m biznotes_core.services.skills.orphans --min-age-hours 72

freeze와 가져오기는 커밋 **전에** 오브젝트를 쓴다. 그래서 가장 최근 오브젝트가 `--min-age-hours`
(기본 24시간)보다 새 prefix는 진행 중인 쓰기일 수 있어 건너뛴다. 운영 설치에서는 미리보기 결과를
사람이 확인한 뒤 `--apply`로 실행한다. DB·스토리지 접속은 코어 설정(환경 변수)을 따른다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from biznotes_core.config import get_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Skill, SkillVersion
from biznotes_core.services import storage
from biznotes_core.services.skills import package as pkg

DEFAULT_MIN_AGE = timedelta(hours=24)
_ID_QUERY_CHUNK = 1000


@dataclass(frozen=True)
class OrphanPrefix:
    """DB 행이 없는 패키지 prefix 하나."""

    prefix: str
    object_count: int
    newest: datetime | None


@dataclass(frozen=True)
class OrphanScan:
    orphans: list[OrphanPrefix]
    #: DB 행은 없지만 최근에 쓰여 건너뛴 prefix
    recent: list[OrphanPrefix]


def _bucket() -> str:
    return get_settings().storage_bucket_skill_packages


def _group(kind: str) -> dict[uuid.UUID, OrphanPrefix]:
    """`<kind>/<uuid>/...` 오브젝트를 id별로 묶는다. 형식이 다른 key는 정리 대상이 아니다."""
    root = f"{kind}/"
    times: dict[uuid.UUID, list[datetime | None]] = {}
    for key, modified in storage.list_object_times(_bucket(), root):
        head, sep, _ = key[len(root):].partition("/")
        if not sep:
            continue
        try:
            ident = uuid.UUID(head)
        except ValueError:
            continue
        if str(ident) != head:  # 대소문자·하이픈이 다른 key는 우리가 만든 prefix가 아니다
            continue
        times.setdefault(ident, []).append(modified)
    groups: dict[uuid.UUID, OrphanPrefix] = {}
    for ident, stamps in times.items():
        # 시각을 모르는 오브젝트가 하나라도 있으면 최근으로 본다(보수적)
        known = [t for t in stamps if t is not None]
        newest = max(known) if len(known) == len(stamps) else None
        groups[ident] = OrphanPrefix(f"{root}{ident}/", len(stamps), newest)
    return groups


async def _existing(db: AsyncSession, column, ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    pending = list(ids)
    found: set[uuid.UUID] = set()
    for start in range(0, len(pending), _ID_QUERY_CHUNK):
        chunk = pending[start : start + _ID_QUERY_CHUNK]
        found.update((await db.execute(select(column).where(column.in_(chunk)))).scalars())
    return found


async def find_orphan_prefixes(
    db: AsyncSession, *, now: datetime | None = None, min_age: timedelta = DEFAULT_MIN_AGE
) -> OrphanScan:
    """DB에 행이 없는 draft·version prefix를 찾는다. 아무것도 지우지 않는다."""
    cutoff = (now or datetime.now(UTC)) - min_age
    orphans: list[OrphanPrefix] = []
    recent: list[OrphanPrefix] = []
    for kind, column in ((pkg.DRAFT_PREFIX, Skill.id), (pkg.VERSION_PREFIX, SkillVersion.id)):
        groups = await anyio.to_thread.run_sync(_group, kind)
        live = await _existing(db, column, groups)
        for ident in sorted(groups.keys() - live, key=str):
            entry = groups[ident]
            if entry.newest is not None and entry.newest <= cutoff:
                orphans.append(entry)
            else:
                recent.append(entry)
    return OrphanScan(orphans=orphans, recent=recent)


def delete_orphan_prefixes(orphans: Iterable[OrphanPrefix]) -> dict[str, list[str]]:
    """찾은 prefix를 지운다. 반환은 prefix → 지우지 못한 key(없으면 빈 목록)."""
    bucket = _bucket()
    return {o.prefix: storage.delete_prefix(bucket, o.prefix) for o in orphans}


def _entry(o: OrphanPrefix) -> dict[str, object]:
    return {
        "prefix": o.prefix,
        "objects": o.object_count,
        "newest": o.newest.isoformat() if o.newest else None,
    }


async def run(*, apply: bool, min_age: timedelta = DEFAULT_MIN_AGE) -> dict[str, object]:
    async with get_session_factory()() as db:
        scan = await find_orphan_prefixes(db, min_age=min_age)
    report: dict[str, object] = {
        "bucket": _bucket(),
        "applied": apply,
        "min_age_hours": min_age.total_seconds() / 3600,
        "orphans": [_entry(o) for o in scan.orphans],
        "skipped_recent": [_entry(o) for o in scan.recent],
    }
    if apply:
        failed = await anyio.to_thread.run_sync(delete_orphan_prefixes, scan.orphans)
        report["failed"] = {prefix: keys for prefix, keys in failed.items() if keys}
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="스킬 패키지 고아 오브젝트 정리(기본: 미리보기)")
    parser.add_argument("--apply", action="store_true", help="찾은 고아 prefix를 실제로 지운다")
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=DEFAULT_MIN_AGE.total_seconds() / 3600,
        help="가장 최근 오브젝트가 이보다 새 prefix는 건너뛴다(기본 24)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.min_age_hours < 0:
        raise SystemExit("--min-age-hours는 0 이상이어야 합니다")
    report = asyncio.run(run(apply=args.apply, min_age=timedelta(hours=args.min_age_hours)))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report.get("failed") else 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
