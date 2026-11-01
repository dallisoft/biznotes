# 신규(#133, docs/plans/notebook-artifacts/). 필요: PostgreSQL·Valkey(PG 테스트).
"""노트북 산출물 경계 질의 — 실제 PostgreSQL JSONB(`jsonb_array_elements`·DISTINCT ON)로 확인한다.

수락 기준 1·3·6. 도구 경로(`artifact_list`·`artifact_read`)는 실제 세션·권한 조회로 돌리고
스토리지만 가짜다. `create_all` 기반이라 Alembic 마이그레이션 검증이 아니다.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from biznotes_core.models import Agent, AgentRun, Notebook, User
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.errors import ToolUnavailableError
from biznotes_core.services.agents.nodes.artifact_tools import NOT_VISIBLE
from biznotes_core.services.agents.nodes.llm import _dispatch_builtin_tool
from biznotes_core.services.agents.notebook_artifacts import (
    get_notebook_artifact,
    list_notebook_artifacts,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _keys(run_id: uuid.UUID, *names: str) -> list[dict[str, Any]]:
    return [
        {"key": f"{run_id}/{i}/{n}", "name": n, "content_type": "text/markdown"}
        for i, n in enumerate(names)
    ]


class World:
    """노트북 둘(user_a 소유), 실행 사용자별 채팅·비채팅 run."""

    def __init__(self, db: Any, user_a: User, user_b: User, admin: User) -> None:
        self.db, self.a, self.b, self.admin = db, user_a, user_b, admin
        self.runs: dict[str, AgentRun] = {}

    async def build(self) -> "World":
        self.agent = Agent(name="a", kind="react", definition={}, params_schema={}, scope="system")
        self.nb = Notebook(owner_id=self.a.id, title="nb")
        self.other_nb = Notebook(owner_id=self.a.id, title="other")
        self.db.add_all([self.agent, self.nb, self.other_nb])
        await self.db.flush()
        spec = [
            # 이름, 사용자, trigger, 노트북, 산출물(기록 순서)
            ("a_manual", self.a, "manual", self.nb, ("report.md", "data.csv")),
            ("a_chat", self.a, "chat", self.nb, ("chat-a.md",)),
            ("b_chat", self.b, "chat", self.nb, ("chat-b.md",)),
            ("b_schedule", self.b, "schedule", self.nb, ("report.md", "report.md")),
            ("other_nb", self.a, "manual", self.other_nb, ("elsewhere.md",)),
            ("empty", self.a, "manual", self.nb, ()),
        ]
        for i, (key, user, trigger, nb, names) in enumerate(spec):
            run = AgentRun(
                agent_id=self.agent.id,
                notebook_id=nb.id,
                user_id=user.id,
                trigger=trigger,
                status="succeeded",
                created_at=T0 + timedelta(minutes=i),
            )
            self.db.add(run)
            await self.db.flush()
            run.artifact_keys = _keys(run.id, *names)
            self.runs[key] = run
        # 형식이 어긋난 기록 — 질의가 오류 없이 건너뛰어야 한다
        broken = AgentRun(
            agent_id=self.agent.id,
            notebook_id=self.nb.id,
            user_id=self.a.id,
            status="succeeded",
            created_at=T0 + timedelta(minutes=30),
            artifact_keys=[
                {"name": "no-key.md"},
                "string",
                {"key": "", "name": "empty-key.md"},
                {"key": 5, "name": 7},  # 숫자 — 읽을 수 없으니 목록에도 나오지 않는다
            ],
        )
        # 배열이 아닌 기록 — jsonb_array_elements가 오류를 내지 않아야 한다
        not_array = [
            AgentRun(
                agent_id=self.agent.id,
                notebook_id=self.nb.id,
                user_id=self.a.id,
                status="succeeded",
                created_at=T0 + timedelta(minutes=31 + i),
                artifact_keys=value,
            )
            for i, value in enumerate(({"name": "x", "key": "y"}, "text", 3))
        ]
        self.db.add_all([broken, *not_array])
        await self.db.flush()
        # JSON null은 ORM이 SQL NULL로 바꾸므로 직접 넣는다
        await self.db.execute(
            text("UPDATE agent_runs SET artifact_keys = 'null'::jsonb WHERE id = :id"),
            {"id": not_array[2].id},
        )
        await self.db.commit()
        return self

    async def names(self, user: User | None, **kw: Any) -> set[tuple[str, str]]:
        items, _ = await list_notebook_artifacts(
            self.db,
            self.nb.id,
            user_id=user.id if user else None,
            user_role=user.role if user else "user",
            **kw,
        )
        by_id = {r.id: k for k, r in self.runs.items()}
        return {(by_id[a.run_id], a.name) for a in items}


@pytest.fixture
async def world(db: Any, user_a: User, user_b: User, admin_user: User) -> World:
    return await World(db, user_a, user_b, admin_user).build()


async def test_list_follows_the_run_read_rule(world: World) -> None:
    public = {
        ("a_manual", "report.md"),
        ("a_manual", "data.csv"),
        ("b_schedule", "report.md"),  # 같은 이름 두 번 → 하나
    }
    assert await world.names(world.a) == public | {("a_chat", "chat-a.md")}
    assert await world.names(world.b) == public | {("b_chat", "chat-b.md")}
    assert await world.names(world.admin) == public | {
        ("a_chat", "chat-a.md"),
        ("b_chat", "chat-b.md"),
    }
    # 시스템 실행에는 채팅 run이 보이지 않는다
    assert await world.names(None) == public
    # 결과가 노트북 전체에 보이는 곳으로 갈 때(채팅이 아닌 run) — 관리자도 채팅 run을 빼고 본다
    assert await world.names(world.admin, include_chat_runs=False) == public


async def test_list_order_filter_limit_and_exclusion(world: World) -> None:
    items, truncated = await list_notebook_artifacts(
        world.db, world.nb.id, user_id=world.a.id, user_role="user", limit=2
    )
    # 최신 run부터, 같은 run 안에서는 기록 순서
    assert [(a.run_id, a.name) for a in items] == [
        (world.runs["b_schedule"].id, "report.md"),
        (world.runs["a_chat"].id, "chat-a.md"),
    ]
    assert items[0].key == f"{world.runs['b_schedule'].id}/0/report.md"  # 같은 이름은 첫 것
    assert truncated is True

    assert await world.names(world.a, name_contains="REPORT") == {
        ("a_manual", "report.md"),
        ("b_schedule", "report.md"),
    }
    # LIKE 와일드카드는 글자 그대로 찾는다
    assert await world.names(world.a, name_contains="%") == set()
    assert ("a_manual", "report.md") not in await world.names(
        world.a, exclude_run_id=world.runs["a_manual"].id
    )


async def test_get_returns_the_first_entry_and_hides_out_of_bounds(world: World) -> None:
    run = world.runs["b_schedule"]
    art = await get_notebook_artifact(
        world.db, world.nb.id, run.id, "report.md", user_id=world.a.id, user_role="user"
    )
    assert art is not None and art.key == f"{run.id}/0/report.md"  # 첫 것

    async def get(key: str, user: User, nb: Notebook | None = None, **kw: Any) -> Any:
        r = world.runs[key]
        name = r.artifact_keys[0]["name"]
        return await get_notebook_artifact(
            world.db, (nb or world.nb).id, r.id, name, user_id=user.id, user_role=user.role, **kw
        )

    assert await get("b_chat", world.a) is None  # 남의 채팅
    assert await get("b_chat", world.admin) is not None  # 관리자 예외(D1)
    assert await get("b_chat", world.admin, include_chat_runs=False) is None
    assert await get("other_nb", world.a) is None  # 다른 노트북의 run
    assert await get("other_nb", world.a, nb=world.other_nb) is not None


def _ctx(world: World, user: User, trigger: str = "manual") -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        notebook_id=world.nb.id,
        db=world.db,
        params={},
        trigger=trigger,
        user_id=user.id,
        user_role=user.role,
    )


async def test_tools_run_on_postgres_and_recheck_notebook_access(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biznotes_core.services import storage

    class Stream:
        def chunks(self) -> Any:
            yield "# 보고서".encode()

        def close(self) -> None:
            pass

    monkeypatch.setattr(storage, "open_object_stream", lambda _b, _k: Stream())

    listed = json.loads(
        await _dispatch_builtin_tool(_ctx(world, world.a, "chat"), "artifact_list", {})
    )
    assert {a["name"] for a in listed["artifacts"]} == {
        "report.md",
        "data.csv",
        "chat-a.md",
    }
    run_id = str(world.runs["a_manual"].id)
    read = json.loads(
        await _dispatch_builtin_tool(
            _ctx(world, world.a), "artifact_read", {"run_id": run_id, "name": "report.md"}
        )
    )
    assert read["text"] == "# 보고서"

    # user_b는 이 노트북 권한이 없다 — 공유가 없거나 회수된 경우와 같다
    for name, args in (
        ("artifact_list", {}),
        ("artifact_read", {"run_id": run_id, "name": "report.md"}),
    ):
        with pytest.raises(ToolUnavailableError) as exc:
            await _dispatch_builtin_tool(_ctx(world, world.b), name, args)
        assert str(exc.value) == NOT_VISIBLE
