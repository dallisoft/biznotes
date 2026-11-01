# 신규(#402). 필요: PostgreSQL·Valkey(PG 테스트).
"""기존 가져온 스킬 다시 가져오기(#402 §4) — 권한·draft 교체·포인터 불변·잠금 순서.

라우트 함수를 실제 PostgreSQL로 직접 부른다. 스토리지는 같은 함수 표면을 가진 메모리 대역으로
바꾼다. `create_all` 기반이라 Alembic 마이그레이션 검증이 아니다(스키마 변경도 없다).
"""

import asyncio
import contextlib
import io
import threading
import uuid
import zipfile
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select, text

from biznotes_core.api.v1 import skills as skills_api
from biznotes_core.config import reset_settings
from biznotes_core.core.db import get_session_factory
from biznotes_core.models import AuditLog, Skill, SkillReviewRequest, SkillVersion, User
from biznotes_core.schemas.skill import (
    SkillCreate,
    SkillFreezeRequest,
    SkillImportUrlCandidatesRequest,
    SkillImportUrlRequest,
)
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import report as rpt
from biznotes_core.services.skills import sandbox
from biznotes_core.services.skills import service as svc

SKILL_MD = "---\nname: tool\ndescription: 도구 스킬\n---\n# t\n".encode()
V1 = {
    "tool/SKILL.md": SKILL_MD,
    "tool/scripts/run.py": b"print(1)\n",
    "tool/references/old.md": b"old",
}
NESTED = "repo-main/skills/tool/SKILL.md"
V2 = {
    NESTED: SKILL_MD,
    "repo-main/skills/tool/scripts/run.py": b"print(2)\n",
    "repo-main/skills/tool/references/new.md": b"new",
    "repo-main/.github/workflows/test.yml": b"on: push\n",
}
URL = "https://example.test/repo/zip/0123abc"
REF = "github:owner/repo@0123abc:skills/tool"


@pytest.fixture(autouse=True)
def _standard_runtime_on(monkeypatch):
    """freeze가 표준 version을 만들도록 표준 런타임을 켠다(기본 꺼짐)."""
    monkeypatch.setenv("SKILL_STANDARD_RUNTIME_ENABLED", "true")
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(autouse=True)
def objects(monkeypatch) -> dict[str, bytes]:
    """`biznotes_core.services.storage`의 동기 함수 대역(단일 버킷)."""
    store: dict[str, bytes] = {}
    target = "biznotes_core.services.storage"

    def put_object(bucket, key, data, content_type="application/octet-stream"):
        store[key] = data

    def delete_prefix(bucket, prefix):
        for key in [k for k in store if k.startswith(prefix)]:
            del store[key]
        return []

    def copy_object(bucket, src, dst):
        store[dst] = store[src]

    monkeypatch.setattr(f"{target}.put_object", put_object)
    monkeypatch.setattr(f"{target}.get_object", lambda bucket, key: store[key])
    monkeypatch.setattr(f"{target}.copy_object", copy_object)
    monkeypatch.setattr(
        f"{target}.list_objects",
        lambda bucket, prefix: sorted(k for k in store if k.startswith(prefix)),
    )
    monkeypatch.setattr(f"{target}.delete_prefix", delete_prefix)
    return store


def upload(files: dict[str, bytes]) -> UploadFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return UploadFile(file=io.BytesIO(buf.getvalue()), filename="skill.zip")


def draft(store: dict[str, bytes], skill_id: uuid.UUID) -> dict[str, bytes]:
    prefix = pkg.draft_prefix(skill_id)
    return {k[len(prefix) :]: v for k, v in store.items() if k.startswith(prefix)}


async def _fresh(skill_id: uuid.UUID) -> Skill:
    async with get_session_factory()() as s:
        row = await s.get(Skill, skill_id)
        assert row is not None
        return row


def _pointers(skill: Skill) -> tuple[Any, ...]:
    return (
        skill.slug,
        skill.name,
        skill.visibility,
        skill.enabled,
        skill.origin,
        skill.current_version_id,
        skill.published_version_id,
    )


async def _wait_until_blocked_by(holder_pid: int) -> None:
    """다른 연결이 `holder_pid`의 잠금 때문에 기다리는 상태가 될 때까지 기다린다(상한 10초)."""
    async with asyncio.timeout(10), get_session_factory()() as s:
        while not await s.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND :holder = ANY(pg_blocking_pids(pid))"
            ),
            {"holder": holder_pid},
        ):
            await s.rollback()


@pytest.fixture
def no_fetch(monkeypatch) -> None:
    def fail(_url: str) -> bytes:
        raise AssertionError("대상 권한이 없으면 원격 archive를 받지 않는다")

    monkeypatch.setattr(svc, "_fetch_import_archive", fail)


async def test_owner_reimport_replaces_only_the_draft(db, user_a: User, objects) -> None:
    imported = await skills_api.import_skill_zip(
        file=upload(V1), selected_skill_path=None, user=user_a, db=db
    )
    v1 = await skills_api.freeze_skill(
        skill_id=imported.id, body=SkillFreezeRequest(), user=user_a, db=db
    )
    db.add(
        SkillReviewRequest(
            version_id=v1.version_id, kind="scripts", status="approved", requested_by=user_a.id
        )
    )
    skill = await db.get(Skill, imported.id)
    assert skill is not None
    # 복제로 생긴 가져온 스킬처럼 계보를 둔다 — 재가져오기가 보존해야 한다
    skill.origin_detail = {**skill.origin_detail, "lineage": ["v0"], "cloned_from": "src"}
    await db.commit()
    before = _pointers(await _fresh(imported.id))

    out = await skills_api.import_skill_zip(
        file=upload(V2),
        selected_skill_path=NESTED,
        target_skill_id=imported.id,
        source_ref=REF,
        user=user_a,
        db=db,
    )

    assert out.id == imported.id and out.slug == imported.slug
    assert out.import_report["reimported"] is True
    assert out.wrapper_agent_id == v1.wrapper_agent_id  # 기존 wrapper를 그대로 보여 준다
    after = await _fresh(imported.id)
    assert _pointers(after) == before
    assert after.origin_detail == {
        "lineage": ["v0"],
        "cloned_from": "src",
        "source": "zip",
        "source_ref": REF,
        "root_name": "tool",
    }
    assert draft(objects, imported.id) == {
        "SKILL.md": SKILL_MD,
        "scripts/run.py": b"print(2)\n",
        "references/new.md": b"new",
    }
    owned = select(func.count()).select_from(Skill).where(Skill.owner_id == user_a.id)
    assert await db.scalar(owned) == 1  # 새 스킬을 만들지 않는다
    audits = (
        await db.execute(select(AuditLog.detail).where(AuditLog.action == "skill.import"))
    ).scalars()
    assert sorted(str(detail.get("reimport")) for detail in audits) == ["None", "True"]

    # freeze → N+1. scripts 승인은 version 단위라 새 version은 다시 검토가 필요하다
    v2 = await skills_api.freeze_skill(
        skill_id=imported.id, body=SkillFreezeRequest(), user=user_a, db=db
    )
    assert v2.version == 2
    assert await sandbox.scripts_approved(db, v1.version_id) is True
    assert await sandbox.scripts_approved(db, v2.version_id) is False


async def test_org_visibility_does_not_block_the_owner(db, user_a: User, objects) -> None:
    """D2 — 공개 범위와 무관하게 소유자의 가져온 스킬이면 된다(공개는 version 단위)."""
    imported = await skills_api.import_skill_zip(
        file=upload(V1), selected_skill_path=None, user=user_a, db=db
    )
    skill = await db.get(Skill, imported.id)
    assert skill is not None
    skill.visibility = "org"
    await db.commit()

    out = await skills_api.import_skill_zip(
        file=upload(V2),
        selected_skill_path=NESTED,
        target_skill_id=imported.id,
        user=user_a,
        db=db,
    )

    assert out.import_report["reimported"] is True
    assert (await _fresh(imported.id)).visibility == "org"
    assert "references/new.md" in draft(objects, imported.id)


async def test_reimport_target_must_be_the_owners_imported_skill(
    db, user_a: User, user_b: User, no_fetch
) -> None:
    imported = await skills_api.import_skill_zip(
        file=upload(V1), selected_skill_path=None, user=user_a, db=db
    )
    authored = await skills_api.create_skill(
        body=SkillCreate(name="직접 만든 스킬"), user=user_a, db=db
    )
    seed = Skill(
        owner_id=None,
        name="seed",
        description="",
        slug="seed",
        visibility="system",
        origin="system_seed",
    )
    db.add(seed)
    await db.commit()

    cases = [
        (user_b, imported.id, 404),  # 다른 사용자의 스킬 — 존재를 숨긴다
        (user_a, seed.id, 404),  # system 시드(소유자 없음)
        (user_a, uuid.uuid4(), 404),
        (user_a, authored.id, 409),
    ]
    for user, target, code in cases:
        with pytest.raises(HTTPException) as exc:
            await skills_api.import_skill_url(
                body=SkillImportUrlRequest(url=URL, target_skill_id=target, source_ref=REF),
                user=user,
                db=db,
            )
        assert exc.value.status_code == code
    assert str(exc.value.detail).startswith("[target_not_imported]")

    with pytest.raises(HTTPException) as exc:
        await skills_api.import_skill_zip(
            file=upload(V2),
            selected_skill_path=NESTED,
            target_skill_id=imported.id,
            user=user_b,
            db=db,
        )
    assert exc.value.status_code == 404


async def test_url_reimport_pins_the_archive_and_keeps_the_skill(
    db, user_a: User, objects, monkeypatch
) -> None:
    imported = await skills_api.import_skill_zip(
        file=upload(V1), selected_skill_path=None, user=user_a, db=db
    )
    archive = upload(V2).file.read()
    monkeypatch.setattr(svc, "_fetch_import_archive", lambda _url: archive)
    listing = await skills_api.list_skill_url_candidates(
        body=SkillImportUrlCandidatesRequest(url=URL), user=user_a
    )

    out = await skills_api.import_skill_url(
        body=SkillImportUrlRequest(
            url=URL,
            sha256=listing.archive_sha256,
            selected_skill_path=NESTED,
            target_skill_id=imported.id,
            source_ref=REF,
        ),
        user=user_a,
        db=db,
    )

    assert out.id == imported.id and out.import_report["reimported"] is True
    detail = (await _fresh(imported.id)).origin_detail
    assert detail == {
        "source": "url",
        "archive_sha256": listing.archive_sha256,
        "source_ref": REF,
        "root_name": "tool",
    }
    assert URL not in str(detail)


async def test_reimport_validates_before_locking_the_skill_row(db, user_a: User, objects) -> None:
    """검증 실패는 잠금을 기다리지 않고, 통과한 archive는 스킬 행 잠금을 기다린 뒤 바꾼다."""
    imported = await skills_api.import_skill_zip(
        file=upload(V1), selected_skill_path=None, user=user_a, db=db
    )
    task: asyncio.Task | None = None
    try:
        async with get_session_factory()() as holder:
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            await holder.get(Skill, imported.id, with_for_update=True)

            async with asyncio.timeout(5):
                with pytest.raises(HTTPException) as exc:
                    await skills_api.import_skill_zip(
                        file=upload({"tool/SKILL.md": b"---\ndescription: d\n---\n"}),
                        selected_skill_path=None,
                        target_skill_id=imported.id,
                        user=user_a,
                        db=db,
                    )
            assert exc.value.status_code == 422

            task = asyncio.create_task(
                skills_api.import_skill_zip(
                    file=upload(V2),
                    selected_skill_path=NESTED,
                    target_skill_id=imported.id,
                    user=user_a,
                    db=db,
                )
            )
            await _wait_until_blocked_by(holder_pid)
            assert not task.done()
            assert "references/old.md" in draft(objects, imported.id)  # 잠금 전에는 그대로
            await holder.commit()
        async with asyncio.timeout(10):
            out = await task
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert out.import_report["reimported"] is True
    assert "references/new.md" in draft(objects, imported.id)
    assert "references/old.md" not in draft(objects, imported.id)


# ---------- draft를 만지는 경로의 직렬화(리뷰 H1) ----------
# freeze는 draft를 두 번 읽는다(검증·해시용, version prefix 복사용). 그 사이에 재가져오기가 draft를
# 바꾸면 V1을 검증·해시하고 V2 파일을 동결했다. draft를 쓰는 경로가 모두 스킬 행을 먼저 잠근다.


async def _wait_until_lock_wait() -> None:
    """어떤 연결이든 행 잠금을 기다리는 상태가 될 때까지 기다린다(상한 10초)."""
    async with asyncio.timeout(10), get_session_factory()() as s:
        while not await s.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND cardinality(pg_blocking_pids(pid)) > 0"
            )
        ):
            await s.rollback()


def _assert_consistent(version: SkillVersion, files: dict[str, bytes]) -> None:
    """동결된 파일과 version 행의 해시·파일 트리·보고서가 같은 패키지에서 나왔다."""
    assert version.package_hash == pkg.package_hash(files)
    assert version.skill_md_hash == pkg.file_hash(files["SKILL.md"])
    assert version.file_tree == pkg.build_file_tree(files)
    assert version.security_report == rpt.evaluate_package(files).report


async def _reimport_in_own_session(user_id: uuid.UUID, skill_id: uuid.UUID) -> None:
    unpacked = pkg.unpack_zip_detailed(upload(V2).file.read(), selected_skill_path=NESTED)
    async with get_session_factory()() as other:
        owner = await other.get(User, user_id)
        await svc._reimport_unpacked(
            other, owner, skill_id, unpacked, origin_detail={"source": "zip"}
        )
        await other.commit()


async def test_reimport_between_freeze_reads_waits_for_the_freeze(
    db, user_a: User, objects, monkeypatch
) -> None:
    """freeze가 검증용으로 draft를 읽은 직후 들어온 재가져오기는 freeze 커밋을 기다린다."""
    imported = await skills_api.import_skill_zip(
        file=upload(V1), selected_skill_path=None, user=user_a, db=db
    )
    v1_files = draft(objects, imported.id)
    validated = threading.Event()
    resume = threading.Event()
    real_load = pkg.load_draft_files

    def load_then_pause(skill_id: uuid.UUID) -> dict[str, bytes]:
        files = real_load(skill_id)
        if not validated.is_set():  # freeze의 첫 읽기(검증·해시용)만 멈춘다
            validated.set()
            resume.wait(10)
        return files

    monkeypatch.setattr(pkg, "load_draft_files", load_then_pause)
    freeze: asyncio.Task | None = None
    reimport: asyncio.Task | None = None
    try:
        freeze = asyncio.create_task(
            skills_api.freeze_skill(
                skill_id=imported.id, body=SkillFreezeRequest(), user=user_a, db=db
            )
        )
        assert await asyncio.to_thread(validated.wait, 10)
        reimport = asyncio.create_task(_reimport_in_own_session(user_a.id, imported.id))
        await _wait_until_lock_wait()  # 재가져오기가 freeze의 스킬 행 잠금을 기다린다
        assert not reimport.done()
        resume.set()
        async with asyncio.timeout(10):
            frozen = await freeze
            await reimport
    finally:
        resume.set()
        for task in (freeze, reimport):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    version = await db.get(SkillVersion, frozen.version_id, populate_existing=True)
    frozen_files = pkg.load_version_files(frozen.version_id)
    assert frozen_files == v1_files  # 검증한 패키지 그대로 동결된다
    _assert_consistent(version, frozen_files)
    assert "references/new.md" in draft(objects, imported.id)  # 재가져오기는 그 뒤에 반영된다


async def test_freeze_waits_for_reimport_and_freezes_the_new_package(
    db, user_a: User, objects, monkeypatch
) -> None:
    """재가져오기가 잠금을 쥔 동안 freeze는 draft를 읽지 않고 기다리고, 끝난 뒤 V2를 동결한다."""
    imported = await skills_api.import_skill_zip(
        file=upload(V1), selected_skill_path=None, user=user_a, db=db
    )
    unpacked = pkg.unpack_zip_detailed(upload(V2).file.read(), selected_skill_path=NESTED)
    reads: list[uuid.UUID] = []
    real_load = pkg.load_draft_files

    def counting_load(skill_id: uuid.UUID) -> dict[str, bytes]:
        reads.append(skill_id)
        return real_load(skill_id)

    monkeypatch.setattr(pkg, "load_draft_files", counting_load)
    task: asyncio.Task | None = None
    try:
        async with get_session_factory()() as holder:
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            owner = await holder.get(User, user_a.id)
            await svc._reimport_unpacked(
                holder, owner, imported.id, unpacked, origin_detail={"source": "zip"}
            )
            task = asyncio.create_task(
                skills_api.freeze_skill(
                    skill_id=imported.id, body=SkillFreezeRequest(), user=user_a, db=db
                )
            )
            await _wait_until_blocked_by(holder_pid)
            assert not task.done()
            # 잠금을 먼저 잡는다 — 기다리는 동안 draft를 한 번도 읽지 않았다
            assert reads == []
            await holder.commit()
        async with asyncio.timeout(10):
            frozen = await task
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    version = await db.get(SkillVersion, frozen.version_id, populate_existing=True)
    frozen_files = pkg.load_version_files(frozen.version_id)
    assert frozen_files == unpacked.files
    _assert_consistent(version, frozen_files)


async def test_draft_file_write_waits_for_the_skill_row_lock(db, user_a: User, objects) -> None:
    """draft 단건 PUT도 같은 잠금을 기다린다 — freeze 도중 파일이 바뀌지 않는다."""

    class _Body:
        headers = {"content-type": "application/octet-stream"}

        async def stream(self):
            yield b"edited"

    imported = await skills_api.import_skill_zip(
        file=upload(V1), selected_skill_path=None, user=user_a, db=db
    )
    task: asyncio.Task | None = None
    try:
        async with get_session_factory()() as holder:
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            await holder.get(Skill, imported.id, with_for_update=True)
            task = asyncio.create_task(
                skills_api.write_draft_file(
                    skill_id=imported.id,
                    path="references/edit.md",
                    request=_Body(),  # type: ignore[arg-type]
                    user=user_a,
                    db=db,
                )
            )
            await _wait_until_blocked_by(holder_pid)
            assert "references/edit.md" not in draft(objects, imported.id)
            await holder.commit()
        async with asyncio.timeout(10):
            await task
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert draft(objects, imported.id)["references/edit.md"] == b"edited"
