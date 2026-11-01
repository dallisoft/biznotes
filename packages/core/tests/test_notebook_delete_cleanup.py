"""노트북 삭제의 스토리지 정리(#177) — 하위 소스·run 산출물·내보내기 오브젝트를 커밋 뒤에 지운다.

권한 판정과 키 수집·일괄 삭제(`storage.delete_objects`)는 실경로이고,
DB 세션과 S3 백엔드의 minio 클라이언트만 가짜다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from conftest import FakeResult
from fastapi import HTTPException

from biznotes_core.api.v1 import notebooks
from biznotes_core.config import get_settings
from biznotes_core.models import Notebook, User
from biznotes_core.services import storage


class FakeMinio:
    """버킷별 오브젝트 집합. `remove_objects`는 실제 클라이언트처럼 lazy하게 거부 항목만 낸다."""

    def __init__(self, objects: dict[str, set[str]], rejected: set[str] | None = None) -> None:
        self.objects = objects
        self.rejected = rejected or set()
        self.calls: list[tuple[str, list[str]]] = []
        self.timeline: list[str] | None = None

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.objects

    def remove_objects(self, bucket: str, objects: Any):
        names = [obj.name for obj in objects]
        self.calls.append((bucket, names))
        if self.timeline is not None:
            self.timeline.append(f"remove:{bucket}")
        for name in names:
            if name in self.rejected:
                yield SimpleNamespace(name=name, code="AccessDenied")
            else:
                self.objects[bucket].discard(name)


class FakeDB:
    def __init__(
        self,
        notebook: Notebook,
        sources: list[tuple[Any, ...]],
        run_artifacts: list[Any],
        timeline: list[str],
    ) -> None:
        self.notebook = notebook
        self.sources = sources
        self.run_artifacts = run_artifacts
        self.timeline = timeline
        self.shares: list[str] = []

    async def get(self, model: Any, key: Any) -> Any:
        return self.notebook if model is Notebook and key == self.notebook.id else None

    async def execute(self, stmt: Any) -> FakeResult:
        sql = str(stmt)
        if "FROM sources" in sql:
            assert self.notebook.id in stmt.compile().params.values()
            return FakeResult(self.sources)
        if "FROM agent_runs" in sql:
            return FakeResult([(a,) for a in self.run_artifacts])
        if "notebook_shares" in sql:
            return FakeResult([(p,) for p in self.shares])
        return FakeResult([])  # user_groups

    async def delete(self, obj: Any) -> None:
        self.timeline.append("db.delete")

    async def commit(self) -> None:
        self.timeline.append("db.commit")


@pytest.fixture
def env(use_s3_client: Any) -> dict[str, Any]:
    s = get_settings()
    owner = User(id=uuid.uuid4(), role="user")
    nb = Notebook(id=uuid.uuid4(), owner_id=owner.id, visibility="private", title="지울 노트북")
    hwp_id, pdf_id, url_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    run_id = uuid.uuid4()
    sources = [
        (hwp_id, f"{hwp_id}/raw.hwp", f"{hwp_id}/parsed.md"),
        (pdf_id, f"{pdf_id}/raw.pdf", f"{pdf_id}/parsed.md"),
        (url_id, None, None),  # 아직 파싱 전인 URL 소스 — 위치 산출물 키만 시도한다
    ]
    run_artifacts = [
        [
            {"key": f"{run_id}/report.md", "name": "report.md", "content_type": "text/markdown"},
            {"key": f"{run_id}/scripts/c1/0", "name": "out.csv", "content_type": "text/csv"},
            {"name": "키 없는 항목"},
            "형식이 어긋난 항목",
        ],
        {"not": "a list"},
        [],
    ]
    others = {
        s.storage_bucket_sources: {"남의/raw.pdf"},
        s.storage_bucket_parsed: {"남의/parsed.md"},
        s.storage_bucket_artifacts: {"남의-run/a.md"},
    }
    objects = {
        s.storage_bucket_sources: {f"{hwp_id}/raw.hwp", f"{pdf_id}/raw.pdf"},
        s.storage_bucket_parsed: {
            f"{hwp_id}/parsed.md",
            f"{pdf_id}/parsed.md",
        },
        s.storage_bucket_artifacts: {
            f"{run_id}/report.md",
            f"{run_id}/scripts/c1/0",
        },
    }
    for bucket, keys in others.items():
        objects[bucket] |= keys
    fake = FakeMinio(objects)
    timeline: list[str] = []
    fake.timeline = timeline
    use_s3_client(fake)
    db = FakeDB(nb, sources, run_artifacts, timeline)
    return {"owner": owner, "nb": nb, "db": db, "minio": fake, "others": others, "run_id": run_id}


async def test_delete_removes_source_and_run_objects_after_commit(env) -> None:
    await notebooks.delete(env["nb"].id, user=env["owner"], db=env["db"])

    # 노트북 소유 오브젝트는 모두 사라지고 남의 오브젝트만 남는다
    assert env["minio"].objects == env["others"]
    # 커밋이 끝난 뒤에 버킷마다 한 번씩 일괄 삭제한다
    timeline = env["db"].timeline
    assert timeline[:2] == ["db.delete", "db.commit"]
    assert sorted(timeline[2:]) == sorted(f"remove:{b}" for b in env["others"])


async def test_non_owner_delete_touches_nothing(env) -> None:
    writer = User(id=uuid.uuid4(), role="user")
    env["db"].shares = ["write"]  # 쓰기 공유여도 삭제는 소유자만

    with pytest.raises(HTTPException) as exc:
        await notebooks.delete(env["nb"].id, user=writer, db=env["db"])

    assert exc.value.status_code == 403
    assert env["db"].timeline == []
    assert env["minio"].calls == []


async def test_storage_failure_is_logged_and_does_not_fail_delete(env, monkeypatch) -> None:
    def broken(bucket: str, keys: list[str]) -> list[str]:
        raise ConnectionError("minio down")

    monkeypatch.setattr(storage, "delete_objects", broken)
    with structlog.testing.capture_logs() as logs:
        await notebooks.delete(env["nb"].id, user=env["owner"], db=env["db"])

    assert env["db"].timeline == ["db.delete", "db.commit"]
    failures = [e for e in logs if e["event"] == "오브젝트 삭제 실패"]
    assert len(failures) == 3  # 버킷마다 한 줄, 키 값 없이 개수만
    assert all("key" not in e and e["count"] > 0 for e in failures)


async def test_rejected_keys_are_logged_one_by_one(env) -> None:
    s = get_settings()
    rejected = f"{env['run_id']}/report.md"
    env["minio"].rejected = {rejected}
    with structlog.testing.capture_logs() as logs:
        await notebooks.delete(env["nb"].id, user=env["owner"], db=env["db"])

    failures = [e for e in logs if e["event"] == "오브젝트 삭제 실패"]
    assert failures == [
        {
            "event": "오브젝트 삭제 실패",
            "log_level": "warning",
            "bucket": s.storage_bucket_artifacts,
            "key": rejected,
        }
    ]


def test_delete_objects_dedups_and_skips_missing_bucket(use_s3_client) -> None:
    fake = FakeMinio({"b": {"x", "y"}}, rejected={"y"})
    use_s3_client(fake)

    assert storage.delete_objects("b", ["x", "y", "x"]) == ["y"]
    assert fake.calls == [("b", ["x", "y"])]
    assert storage.delete_objects("없는-버킷", ["x"]) == []
    assert storage.delete_objects("b", []) == []
    assert len(fake.calls) == 1
