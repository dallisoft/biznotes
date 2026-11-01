"""사용자 삭제의 스토리지 정리(#191) — CASCADE로 사라지는 행들의 오브젝트를 커밋 뒤에 지운다.

키 수집 질의의 조건과 일괄 삭제(`storage.delete_objects`)는 실경로이고,
DB 세션과 S3 백엔드의 minio 클라이언트만 가짜다. 실제 CASCADE는 PG 테스트(`*_pg.py`)가 확인한다.
"""

import uuid
from typing import Any

import pytest
import structlog
from conftest import FakeResult
from fastapi import HTTPException
from test_notebook_delete_cleanup import FakeMinio

from biznotes_core.api.v1.admin import users
from biznotes_core.config import get_settings
from biznotes_core.models import User
from biznotes_core.services import storage


class FakeDB:
    """질의 종류별로 정해 둔 행을 돌려주고, 질의 조건과 쓰기 순서를 기록한다."""

    def __init__(self, target: User, rows: dict[str, list[tuple[Any, ...]]]) -> None:
        self.target = target
        self.rows = rows
        self.timeline: list[str] = []
        self.statements: dict[str, Any] = {}

    async def get(self, model: Any, key: Any, **_kw: Any) -> Any:
        return self.target if model is User and key == self.target.id else None

    def add(self, obj: Any) -> None:
        self.timeline.append(f"add:{type(obj).__name__}")

    async def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:  # 활성 관리자 명부 잠금(#241)
            self.timeline.append("lock")
            return FakeResult()
        if sql.startswith("SELECT users.role, users.is_active"):  # 요청자 재확인(#276)
            self.timeline.append("requester")
            return FakeResult([("admin", True)])
        for table in ("sources", "agent_runs"):
            if f"FROM {table}" in sql:
                self.timeline.append(f"select:{table}")
                self.statements[table] = stmt
                return FakeResult(self.rows[table])
        raise AssertionError(f"예상하지 못한 질의: {sql}")

    async def delete(self, obj: Any) -> None:
        assert obj is self.target
        self.timeline.append("db.delete")

    async def commit(self) -> None:
        self.timeline.append("db.commit")


@pytest.fixture
def env(use_s3_client: Any) -> dict[str, Any]:
    s = get_settings()
    admin = User(id=uuid.uuid4(), role="admin", email="admin@test.dev")
    target = User(id=uuid.uuid4(), role="user", email="gone@test.dev")
    hwp_id, url_id = uuid.uuid4(), uuid.uuid4()
    own_run, foreign_run = uuid.uuid4(), uuid.uuid4()
    rows = {
        "sources": [
            (hwp_id, f"{hwp_id}/raw.hwp", f"{hwp_id}/parsed.md"),
            (url_id, None, None),
        ],
        "agent_runs": [
            ([{"key": f"{own_run}/report.md", "name": "report.md"}],),
            # 다른 사람 노트북에서 실행한 run
            ([{"key": f"{foreign_run}/a.mp3", "name": "a.mp3"}, {"name": "키 없음"}],),
        ],
    }
    others = {
        s.storage_bucket_sources: {"남의/raw.pdf"},
        s.storage_bucket_parsed: {"남의/parsed.md"},
        s.storage_bucket_artifacts: {"남의-run/a.md"},
    }
    objects = {
        s.storage_bucket_sources: {f"{hwp_id}/raw.hwp"},
        s.storage_bucket_parsed: {f"{hwp_id}/parsed.md"},
        s.storage_bucket_artifacts: {
            f"{own_run}/report.md",
            f"{foreign_run}/a.mp3",
        },
    }
    for bucket, keys in others.items():
        objects[bucket] |= keys
    fake = FakeMinio(objects)
    db = FakeDB(target, rows)
    fake.timeline = db.timeline
    use_s3_client(fake)
    return {"admin": admin, "target": target, "db": db, "minio": fake, "others": others}


async def test_delete_user_removes_cascaded_objects_after_commit(env) -> None:
    await users.delete_user(env["target"].id, db=env["db"], admin=env["admin"])

    # 삭제 대상 사용자의 오브젝트는 모두 사라지고 남의 오브젝트만 남는다
    assert env["minio"].objects == env["others"]
    # 키는 삭제 전에 모으고, 커밋이 끝난 뒤 버킷마다 한 번씩 일괄 삭제한다
    timeline = env["db"].timeline
    # 일반 사용자 대상이라 남는 관리자를 세지 않는다(#241)
    assert timeline[:6] == [
        "lock",
        "requester",
        "add:AuditLog",
        "select:sources",
        "select:agent_runs",
        "db.delete",
    ]
    assert timeline[6] == "db.commit"
    assert sorted(timeline[7:]) == sorted(f"remove:{b}" for b in env["others"])


async def test_collection_covers_owned_notebooks_and_foreign_runs(env) -> None:
    await users.delete_user(env["target"].id, db=env["db"], admin=env["admin"])

    target_id = env["target"].id
    stmts = env["db"].statements
    for table, foreign_column in (
        ("sources", None),
        ("agent_runs", "agent_runs.user_id"),
    ):
        sql = str(stmts[table])
        # 노트북마다 질의하지 않고 소유 노트북 서브쿼리 하나로 묶는다
        assert "notebooks.owner_id" in sql and " IN (SELECT notebooks.id" in sql
        if foreign_column:
            # 다른 사람 노트북의 행도 사용자 FK CASCADE로 지워지므로 함께 모은다
            assert f"{foreign_column} = " in sql and " OR " in sql
        assert target_id in stmts[table].compile().params.values()


@pytest.mark.parametrize(
    ("target_is_admin", "status_code"),
    [(True, 422), (False, 404)],
)
async def test_rejected_delete_touches_nothing(env, target_is_admin, status_code) -> None:
    user_id = env["admin"].id if target_is_admin else uuid.uuid4()

    with pytest.raises(HTTPException) as exc:
        await users.delete_user(user_id, db=env["db"], admin=env["admin"])

    assert exc.value.status_code == status_code
    # 자기 삭제는 잠그기 전에, 없는 대상은 잠근 뒤 조회에서 거부한다
    assert env["db"].timeline == ([] if target_is_admin else ["lock", "requester"])
    assert env["minio"].calls == []


async def test_storage_failure_is_logged_and_does_not_fail_delete(env, monkeypatch) -> None:
    def broken(bucket: str, keys: list[str]) -> list[str]:
        raise ConnectionError("minio down")

    monkeypatch.setattr(storage, "delete_objects", broken)
    with structlog.testing.capture_logs() as logs:
        result = await users.delete_user(env["target"].id, db=env["db"], admin=env["admin"])

    assert result is None
    assert env["db"].timeline[-2:] == ["db.delete", "db.commit"]
    failures = [e for e in logs if e["event"] == "오브젝트 삭제 실패"]
    assert len(failures) == 3  # 버킷마다 한 줄, 키 값 없이 개수만
    assert all("key" not in e and e["count"] > 0 for e in failures)
