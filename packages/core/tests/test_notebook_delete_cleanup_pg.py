# 신규(#177). 필요: PostgreSQL·Valkey(PG 테스트).
"""노트북 삭제의 스토리지 정리 — 실제 PostgreSQL에서 키 수집 질의와 CASCADE를 함께 확인한다.

라우터(`notebooks.delete`)는 실제 세션으로 돌리고 S3 백엔드의 minio 클라이언트만 가짜다.
`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select

from biznotes_core.api.v1 import notebooks
from biznotes_core.config import get_settings
from biznotes_core.models import Agent, AgentRun, Notebook, Source, User
from biznotes_core.services.ingestion import stage_artifacts


class _FakeMinio:
    def __init__(self) -> None:
        self.removed: dict[str, set[str]] = {}

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def remove_objects(self, bucket: str, objects: Any):
        self.removed.setdefault(bucket, set()).update(obj.name for obj in objects)
        yield from ()


async def _source(db: Any, nb: Notebook, raw: bool) -> Source:
    src = Source(notebook_id=nb.id, type="file", title="t", status="ready")
    db.add(src)
    await db.flush()
    if raw:
        src.original_object_key = f"{src.id}/raw.hwp"
        src.parsed_object_key = f"{src.id}/parsed.md"
    return src


async def test_delete_collects_keys_from_real_rows_and_cascades(db, user_a: User, use_s3_client):
    fake = _FakeMinio()
    use_s3_client(
        SimpleNamespace(bucket_exists=fake.bucket_exists, remove_objects=fake.remove_objects)
    )
    agent = Agent(name="a", kind="react", definition={}, params_schema={}, scope="system")
    nb, other = Notebook(owner_id=user_a.id, title="nb"), Notebook(owner_id=user_a.id, title="o")
    db.add_all([agent, nb, other])
    await db.flush()
    src, bare = await _source(db, nb, True), await _source(db, nb, False)
    kept = await _source(db, other, True)
    run = AgentRun(agent_id=agent.id, notebook_id=nb.id, user_id=user_a.id, status="succeeded")
    other_run = AgentRun(
        agent_id=agent.id, notebook_id=other.id, user_id=user_a.id, status="succeeded"
    )
    db.add_all([run, other_run])
    await db.flush()
    run.artifact_keys = [
        {"key": f"{run.id}/report.md", "name": "report.md", "content_type": "text/markdown"},
        {"name": "키 없는 항목"},
    ]
    other_run.artifact_keys = [{"key": f"{other_run.id}/x.md", "name": "x.md"}]
    await db.commit()
    nb_id, other_id = nb.id, other.id

    await notebooks.delete(nb_id, user=user_a, db=db)

    s = get_settings()
    assert fake.removed == {
        s.storage_bucket_sources: {src.original_object_key},
        s.storage_bucket_parsed: {
            src.parsed_object_key,
            # 색인 단계로 넘기는 위치 산출물(#244) — 키가 행에 없어도 지운다
            stage_artifacts.locations_key(src.id),
            stage_artifacts.locations_key(bare.id),
        },
        s.storage_bucket_artifacts: {f"{run.id}/report.md"},
    }
    # 하위 행은 CASCADE로 사라지고 다른 노트북은 그대로다
    for model in (Source, AgentRun):
        left = await db.scalar(
            select(func.count()).select_from(model).where(model.notebook_id == nb_id)
        )
        assert left == 0
    assert await db.scalar(select(func.count()).where(Source.notebook_id == other_id)) == 1
    assert kept.original_object_key not in fake.removed[s.storage_bucket_sources]
