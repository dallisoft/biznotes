# 신규(#448). 필요: PostgreSQL·Valkey(PG 테스트).
"""소스가 많을 때 계층 요약(배치 reduce) — 실제 DB 캐시·실제 Redis 배치 캐시.

모델 호출(`complete_stream_with_fallback`)만 대역이다. 1차 모델은 DB에 등록한 32k 기본 모델이라
담을 예산이 `context_window × 0.5`로 정해진다.
"""

import re
import uuid
from typing import Any

import pytest

from biznotes_core.models import LLMModel, LLMProvider, Notebook, Source, SourceChunk, User
from biznotes_core.services import source_digest as sd
from biznotes_core.services.agents.context import RunContext
from biznotes_core.services.agents.nodes.digest import _exec_source_digest
from biznotes_core.services.gateway.llm import StreamResult

DIGEST = ("요약: 핵심 주장, 수치 12%, 3월 출시 연기, 절 구성. " * 20)[:450]
TITLE_LINE = re.compile(r"^문서 제목: (.+)$", re.M)


class FakeLLM:
    """소스 요약 호출에는 DIGEST, 배치 요약 호출에는 제목마다 항목 하나를 돌려준다."""

    def __init__(self) -> None:
        self.digest_calls = 0
        self.batch_calls = 0

    async def __call__(self, db: Any, model_id: Any, messages: Any, result: StreamResult,
                       **kw: Any) -> Any:
        system, user = messages[0]["content"], messages[1]["content"]
        if "개의 요약이다" in system:
            self.batch_calls += 1
            text = "\n".join(f"- {t}: 결론과 수치 12%" for t in TITLE_LINE.findall(user))
        else:
            self.digest_calls += 1
            text = DIGEST
        result.usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

        async def gen() -> Any:
            yield text

        return gen()


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(sd, "complete_stream_with_fallback", fake)
    return fake


async def _setup(db: Any, owner: User, count: int) -> tuple[Notebook, list[Source]]:
    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    db.add(LLMModel(provider_id=provider.id, model_name="m32k", display_name="32k 모델",
                    capabilities=["llm"], is_default=True, enabled=True, context_window=32_768))
    nb = Notebook(owner_id=owner.id, title="많은 소스")
    db.add(nb)
    await db.flush()
    ids = sorted(uuid.uuid4() for _ in range(count))
    sources = [
        Source(id=ids[i], notebook_id=nb.id, type="text", title=f"자료-{i:03d}", status="ready",
               chunk_count=1)
        for i in range(count)
    ]
    db.add_all(sources)
    await db.flush()
    db.add_all(
        SourceChunk(source_id=s.id, seq=0, text=f"{s.title} 본문")
        for s in sources
    )
    await db.commit()
    return nb, sources


async def test_many_sources_are_batched_and_the_second_run_is_free(db, user_a, llm):
    nb, sources = await _setup(db, user_a, 120)

    def ctx() -> RunContext:
        return RunContext(run_id=uuid.uuid4(), notebook_id=nb.id, db=db, params={})

    first = await _exec_source_digest(ctx(), {"id": "digest"}, {})

    assert llm.digest_calls == 120 and llm.batch_calls == len(first["blocks"]) > 1
    assert [s["title"] for s in first["sources"]] == [s.title for s in sources]
    assert all(f"- {s.title}:" in first["context"] for s in sources)
    assert sd.count_tokens(first["context"]) <= 32_768 // 2

    # 소스 요약(DB)과 배치 요약(Redis)이 모두 캐시다 — LLM 호출 0
    second = await _exec_source_digest(ctx(), {"id": "digest"}, {})

    assert (llm.digest_calls, llm.batch_calls) == (120, len(first["blocks"]))
    assert second["context"] == first["context"]
    assert all(b["cached"] for b in second["blocks"])
    assert all(s["cached"] for s in second["sources"])
