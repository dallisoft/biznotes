"""스킬 패키지 오브젝트 정리(#151) — 실제 S3 서버 없이 검증하는 부분.

삭제 순서·CASCADE·고아 판정은 DB가 필요해 `tests/test_skill_delete_objects_pg.py`에 있다.
"""

import uuid
from types import SimpleNamespace

import structlog

from biznotes_core.services import storage
from biznotes_core.services.skills import package as pkg


def test_delete_skill_objects_reports_partial_failures(monkeypatch) -> None:
    """서버가 일부 key를 거부해도 다음 prefix를 계속 지우고, 거부된 key를 경고로 남긴다."""
    skill_id, v1, v2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    calls: list[str] = []

    def delete_prefix(bucket: str, prefix: str) -> list[str]:
        calls.append(prefix)
        if prefix == pkg.version_prefix(v1):
            return [prefix + "SKILL.md"]
        if prefix == pkg.version_prefix(v2):
            raise TimeoutError
        return []

    monkeypatch.setattr(storage, "delete_prefix", delete_prefix)
    with structlog.testing.capture_logs() as logs:
        ok = pkg.delete_skill_objects(skill_id, [v1, v2])

    assert ok is False
    assert calls == [pkg.draft_prefix(skill_id), pkg.version_prefix(v1), pkg.version_prefix(v2)]
    warnings = [e for e in logs if e["event"] == "skill.delete.objects_failed"]
    assert [(w["prefix"], w.get("failed_count"), w.get("error")) for w in warnings] == [
        (pkg.version_prefix(v1), 1, None),
        (pkg.version_prefix(v2), None, "TimeoutError"),
    ]


def test_delete_skill_objects_success_is_quiet(monkeypatch) -> None:
    monkeypatch.setattr(storage, "delete_prefix", lambda bucket, prefix: [])
    with structlog.testing.capture_logs() as logs:
        assert pkg.delete_skill_objects(uuid.uuid4(), [uuid.uuid4()]) is True
    assert logs == []


class _FakeMinio:
    def __init__(self, keys: list[str], rejected: set[str]) -> None:
        self.keys = keys
        self.rejected = rejected

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def list_objects(self, bucket: str, prefix: str, recursive: bool):
        return [
            SimpleNamespace(object_name=k, last_modified=None)
            for k in self.keys
            if k.startswith(prefix)
        ]

    def remove_objects(self, bucket: str, objects):
        # 실제 클라이언트처럼 거부된 항목만 lazy하게 내보낸다
        for obj in objects:
            if obj.name in self.rejected:
                yield SimpleNamespace(name=obj.name, code="AccessDenied")


def test_delete_prefix_returns_rejected_keys(use_s3_client) -> None:
    fake = _FakeMinio(["p/a", "p/b", "q/c"], rejected={"p/b"})
    use_s3_client(fake)
    assert storage.delete_prefix("bucket", "p/") == ["p/b"]
    assert storage.delete_prefix("bucket", "none/") == []
