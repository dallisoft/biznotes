# 신규(#151). 필요: PostgreSQL·Valkey(PG 테스트).
"""스킬 삭제가 draft·모든 version prefix 오브젝트를 지우는지, 고아 정리 도구가 DB 기준으로만
지우는지 검증한다.

CASCADE·행 잠금·커밋 순서가 필요해 PostgreSQL에서 돌린다. 스토리지는 같은 함수 표면을 가진 메모리
대역으로 바꾼다(실 스토리지 경로는 계획·PR의 수동 스모크로 확인). `create_all` 기반이라 Alembic
마이그레이션 검증이 아니다.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from biznotes_core.core.db import get_session_factory
from biznotes_core.models import Skill, SkillVersion, User
from biznotes_core.services.skills import orphans
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import service as svc

OLD = datetime(2026, 1, 1, tzinfo=UTC)


class MemStorage:
    """`biznotes_core.services.storage`의 동기 함수 대역(단일 버킷)."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime | None]] = {}
        self.fail_prefixes: set[str] = set()

    def put(self, key: str, data: bytes = b"x", modified: datetime | None = OLD) -> None:
        self.objects[key] = (data, modified)

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def install(self, monkeypatch) -> None:
        target = "biznotes_core.services.storage"

        def put_object(bucket, key, data, content_type="application/octet-stream"):
            self.put(key, data, datetime.now(UTC))

        def get_object(bucket, key):
            return self.objects[key][0]

        def copy_object(bucket, src, dst):
            self.put(dst, self.objects[src][0], datetime.now(UTC))

        def delete_prefix(bucket, prefix):
            if prefix in self.fail_prefixes:
                raise ConnectionError("storage down")
            for key in self.keys(prefix):
                del self.objects[key]
            return []

        monkeypatch.setattr(f"{target}.put_object", put_object)
        monkeypatch.setattr(f"{target}.get_object", get_object)
        monkeypatch.setattr(f"{target}.copy_object", copy_object)
        monkeypatch.setattr(f"{target}.list_objects", lambda bucket, prefix: self.keys(prefix))
        monkeypatch.setattr(
            f"{target}.list_object_times",
            lambda bucket, prefix: [(k, self.objects[k][1]) for k in self.keys(prefix)],
        )
        monkeypatch.setattr(f"{target}.delete_prefix", delete_prefix)


@pytest.fixture
def mem(monkeypatch) -> MemStorage:
    store = MemStorage()
    store.install(monkeypatch)
    return store


async def _skill(db: Any, owner: User, mem: MemStorage, versions: int) -> tuple[Skill, list]:
    """draft 파일과 version `versions`개(각 version prefix에 파일 2개)를 가진 스킬을 커밋한다."""
    slug = f"s-{uuid.uuid4().hex[:8]}"
    skill = Skill(owner_id=owner.id, name=slug, description="", slug=slug, visibility="private")
    db.add(skill)
    await db.flush()
    mem.put(pkg.draft_prefix(skill.id) + "SKILL.md")
    ids = []
    for number in range(1, versions + 1):
        version = SkillVersion(
            skill_id=skill.id,
            version=number,
            package_uri="",
            skill_md_hash="h",
            package_hash=f"p{number}",
            manifest={},
            file_tree=[],
            security_report={},
            status="draft_frozen",
        )
        db.add(version)
        await db.flush()
        version.package_uri = pkg.version_prefix(version.id)
        mem.put(pkg.version_prefix(version.id) + "SKILL.md")
        mem.put(pkg.version_prefix(version.id) + "references/a.md")
        ids.append(version.id)
    skill.current_version_id = ids[-1] if ids else None
    await db.commit()
    return skill, ids


def _prefixes(skill: Skill, version_ids: list) -> list[str]:
    return [pkg.draft_prefix(skill.id), *(pkg.version_prefix(v) for v in version_ids)]


async def _delete(db: Any, skill: Skill, user: User) -> None:
    from biznotes_core.api.v1 import skills as route

    await route.delete_skill(skill.id, user=user, db=db)


async def _skill_count(skill_id: uuid.UUID) -> int:
    async with get_session_factory()() as s:
        return int(await s.scalar(select(func.count()).where(Skill.id == skill_id)) or 0)


# ---------- 삭제 경로 ----------


async def test_delete_removes_draft_and_every_version_prefix(db, user_a: User, mem) -> None:
    """수락 기준 1 — 삭제 후 그 스킬의 draft·모든 version prefix 오브젝트가 0개다."""
    skill, version_ids = await _skill(db, user_a, mem, versions=3)
    other, other_versions = await _skill(db, user_a, mem, versions=1)

    await _delete(db, skill, user_a)

    assert await _skill_count(skill.id) == 0
    for prefix in _prefixes(skill, version_ids):
        assert mem.keys(prefix) == [], prefix
    # 다른 스킬은 그대로
    for prefix in _prefixes(other, other_versions):
        assert mem.keys(prefix), prefix


async def test_clone_survives_deleting_its_source(db, user_a: User, mem) -> None:
    """수락 기준 3 — 복제는 오브젝트를 복사하므로 원본 삭제가 복제본 파일을 지우지 않는다."""
    src, src_versions = await _skill(db, user_a, mem, versions=1)
    clone = await svc.clone_skill(db, src, user_a)
    await db.commit()
    cloned = mem.keys(pkg.draft_prefix(clone.id))
    assert cloned  # 동결 version 트리를 clone의 draft로 복사했다

    await _delete(db, src, user_a)

    assert mem.keys(pkg.version_prefix(src_versions[0])) == []
    assert mem.keys(pkg.draft_prefix(clone.id)) == cloned
    # version prefix는 version 행마다 자기 id로만 만든다 — 다른 행이 같은 prefix를 가리키지 않는다
    async with get_session_factory()() as s:
        uris = (await s.execute(select(SkillVersion.id, SkillVersion.package_uri))).all()
    assert all(uri == pkg.version_prefix(vid) for vid, uri in uris)


async def test_db_failure_keeps_objects(db, user_a: User, mem, monkeypatch) -> None:
    """수락 기준 2(음성 대조) — DB 삭제가 실패하면 오브젝트를 지우지 않는다."""
    skill, version_ids = await _skill(db, user_a, mem, versions=2)
    skill_id = skill.id  # 롤백 뒤에는 만료된 속성을 읽을 수 없다
    before = mem.keys()

    async def broken_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", broken_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        await _delete(db, skill, user_a)
    await db.rollback()

    assert mem.keys() == before
    assert await _skill_count(skill_id) == 1


async def test_object_failure_is_logged_and_delete_still_succeeds(
    db, user_a: User, mem
) -> None:
    """오브젝트 삭제가 실패해도 DB 삭제는 유지되고(204), 나머지 prefix는 지운다."""
    import structlog

    skill, version_ids = await _skill(db, user_a, mem, versions=2)
    broken = pkg.version_prefix(version_ids[0])
    mem.fail_prefixes.add(broken)

    with structlog.testing.capture_logs() as logs:
        await _delete(db, skill, user_a)

    assert await _skill_count(skill.id) == 0
    assert mem.keys(broken)  # 고아로 남아 정리 도구로 회수한다
    assert mem.keys(pkg.draft_prefix(skill.id)) == []
    assert mem.keys(pkg.version_prefix(version_ids[1])) == []
    warnings = [e for e in logs if e["event"] == "skill.delete.objects_failed"]
    assert [(w["prefix"], w["error"]) for w in warnings] == [(broken, "ConnectionError")]


# ---------- 고아 정리 도구 ----------


async def test_orphan_scan_lists_only_prefixes_without_rows(db, user_a: User, mem) -> None:
    """수락 기준 4 — 미리보기는 아무것도 지우지 않고, DB에 없는 오래된 prefix만 고른다."""
    live, live_versions = await _skill(db, user_a, mem, versions=1)
    gone_version = uuid.uuid4()
    gone_skill = uuid.uuid4()
    fresh_version = uuid.uuid4()
    mem.put(pkg.version_prefix(gone_version) + "SKILL.md")
    mem.put(pkg.version_prefix(gone_version) + "scripts/run.py")
    mem.put(pkg.draft_prefix(gone_skill) + "SKILL.md")
    mem.put(pkg.version_prefix(fresh_version) + "SKILL.md", modified=datetime.now(UTC))
    mem.put("versions/not-a-uuid/SKILL.md")
    mem.put(f"versions/{str(uuid.uuid4()).upper()}/SKILL.md")
    mem.put("versions/stray-file")
    before = mem.keys()

    report = await orphans.run(apply=False)

    assert mem.keys() == before
    assert report["orphans"] == [
        {"prefix": pkg.draft_prefix(gone_skill), "objects": 1, "newest": OLD.isoformat()},
        {"prefix": pkg.version_prefix(gone_version), "objects": 2, "newest": OLD.isoformat()},
    ]
    assert [e["prefix"] for e in report["skipped_recent"]] == [
        pkg.version_prefix(fresh_version)
    ]
    assert "failed" not in report


async def test_orphan_apply_deletes_only_orphans(db, user_a: User, mem) -> None:
    live, live_versions = await _skill(db, user_a, mem, versions=2)
    gone_version = uuid.uuid4()
    fresh_version = uuid.uuid4()
    mem.put(pkg.version_prefix(gone_version) + "SKILL.md")
    mem.put(pkg.version_prefix(fresh_version) + "SKILL.md", modified=datetime.now(UTC))

    report = await orphans.run(apply=True)

    assert report["failed"] == {}
    assert mem.keys(pkg.version_prefix(gone_version)) == []
    assert mem.keys(pkg.version_prefix(fresh_version))
    for prefix in _prefixes(live, live_versions):
        assert mem.keys(prefix), prefix


async def test_orphan_min_age_zero_includes_recent(db, user_a: User, mem) -> None:
    fresh_version = uuid.uuid4()
    mem.put(pkg.version_prefix(fresh_version) + "SKILL.md", modified=datetime.now(UTC))

    report = await orphans.run(apply=False, min_age=timedelta(0))

    assert [e["prefix"] for e in report["orphans"]] == [pkg.version_prefix(fresh_version)]
