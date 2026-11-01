# 신규(#191). 필요: PostgreSQL·Valkey(PG 테스트).
"""사용자 삭제의 스토리지 정리 — 실제 PostgreSQL에서 키 수집 질의와 CASCADE를 함께 확인한다.

라우터(`admin.users.delete_user`)는 실제 세션으로 돌리고 S3 백엔드의 minio 클라이언트만 가짜다.
`create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select

from biznotes_core.api.v1.admin import users
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


async def _source(db: Any, nb: Notebook) -> Source:
    src = Source(notebook_id=nb.id, type="file", title="t", status="ready")
    db.add(src)
    await db.flush()
    src.original_object_key = f"{src.id}/raw.hwp"
    src.parsed_object_key = f"{src.id}/parsed.md"
    return src


async def _run(db: Any, agent: Agent, nb: Notebook, user: User) -> AgentRun:
    run = AgentRun(agent_id=agent.id, notebook_id=nb.id, user_id=user.id, status="succeeded")
    db.add(run)
    await db.flush()
    run.artifact_keys = [{"key": f"{run.id}/report.md", "name": "report.md"}]
    return run


async def test_delete_user_collects_owned_and_foreign_rows_and_cascades(
    db, user_a: User, user_b: User, admin_user: User, use_s3_client
):
    fake = _FakeMinio()
    use_s3_client(
        SimpleNamespace(bucket_exists=fake.bucket_exists, remove_objects=fake.remove_objects)
    )
    agent = Agent(name="a", kind="react", definition={}, params_schema={}, scope="system")
    owned1, owned2 = (
        Notebook(owner_id=user_a.id, title="a1"),
        Notebook(owner_id=user_a.id, title="a2"),
    )
    foreign = Notebook(owner_id=user_b.id, title="b")
    db.add_all([agent, owned1, owned2, foreign])
    await db.flush()
    # 지울 사용자(a)의 소유 노트북 — 남(b)이 실행한 run도 노트북과 함께 사라진다
    gone_sources = [await _source(db, owned1), await _source(db, owned2)]
    gone_runs = [
        await _run(db, agent, owned1, user_a),
        await _run(db, agent, owned2, user_b),
        # 다른 사람 노트북에서 a가 실행한 run — agent_runs.user_id CASCADE
        await _run(db, agent, foreign, user_a),
    ]
    # 남는 것 — b의 노트북에서 b가 만든 행
    kept_source = await _source(db, foreign)
    kept_run = await _run(db, agent, foreign, user_b)
    await db.commit()
    a_id, foreign_id = user_a.id, foreign.id

    assert await users.delete_user(a_id, db=db, admin=admin_user) is None

    s = get_settings()
    assert fake.removed == {
        s.storage_bucket_sources: {src.original_object_key for src in gone_sources},
        s.storage_bucket_parsed: {src.parsed_object_key for src in gone_sources}
        | {stage_artifacts.locations_key(src.id) for src in gone_sources},  # #244
        s.storage_bucket_artifacts: {f"{run.id}/report.md" for run in gone_runs},
    }
    # CASCADE 결과와 수집 범위가 같다 — b의 노트북에는 b의 행만 남는다
    assert await db.scalar(select(func.count()).where(Notebook.owner_id == a_id)) == 0
    assert await db.scalar(select(func.count()).where(AgentRun.user_id == a_id)) == 0
    left_runs = (
        await db.scalars(select(AgentRun.id).where(AgentRun.notebook_id == foreign_id))
    ).all()
    left_sources = (
        await db.scalars(select(Source.id).where(Source.notebook_id == foreign_id))
    ).all()
    assert (left_runs, left_sources) == ([kept_run.id], [kept_source.id])
