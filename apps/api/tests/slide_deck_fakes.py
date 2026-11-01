"""슬라이드 자료 런타임 테스트의 외부 경계 대역(#218) — LLM 게이트웨이와 RAG 검색만 바꾼다.

코어 그래프·노드(source_digest·loop·json_compose·json_validate·rag_search 범위)는 실경로로 돈다.
- `SlideLLM.stream`: 워크플로우 llm 노드(`gateway.llm.complete_stream_with_fallback`) — 프롬프트
  첫 문장으로 개요·장 작성·삽화 묘사를 가려 응답한다. 장 작성 응답은 프롬프트에 렌더된 장 항목을
  읽어 그 장을 쓴다.
- `SlideLLM.digest_stream`: 소스 요약 생성(`source_digest.complete_stream_with_fallback`).
- `SlideSearch.retrieve`: `rag_chat.retrieve_context` — 받은 검색어·범위를 남기고, 범위의 소스마다
  발췌 하나를 돌려준다.
"""

import json
import re
import uuid
from types import SimpleNamespace
from typing import Any

OUTLINE_HEAD = "아래 소스 요약을 바탕으로 슬라이드 자료의 개요를 만드세요."
WRITE_HEAD = "슬라이드 자료의 한 장을 쓰세요."
ILLUSTRATE_HEAD = "아래 슬라이드 자료에 넣을 삽화를 고르세요."
ITEM_RE = re.compile(r'장 항목:\n<untrusted_data kind="tool">\n(.*?)\n</untrusted_data>', re.S)
_BLOCK_RE = re.compile(r'<untrusted_data kind="tool">\n(.*?)\n</untrusted_data>', re.S)


def outline_doc(slides: list[dict], title: str = "커피 입문") -> dict:
    return {"title": title, "format": "detailed", "slides": slides}


def outline_slide(n: int, source_ids: list[str] | None = None, **extra: Any) -> dict:
    slide: dict[str, Any] = {
        "title": f"장 {n}",
        "summary": f"요약 {n}",
        "layout": "bullets",
        "query": f"검색어 {n}",
        **extra,
    }
    if source_ids is not None:
        slide["source_ids"] = source_ids
    return slide


def written_slide(item: dict) -> dict:
    """장 작성 응답 — 한 장짜리 slide_deck 문서(#218)."""
    slide = {
        "layout": item.get("layout") or "bullets",
        "heading": item["title"],
        "bullets": [f"{item['title']} 근거"],
    }
    return {"title": item["title"], "slides": [slide]}


class SlideLLM:
    """llm 노드 응답 대역. `calls`에 (종류, 프롬프트, max_tokens)를 남긴다."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.digest_calls: list[str] = []
        self.outline: dict = outline_doc([outline_slide(n) for n in range(1, 7)])
        self.images: Any = []
        # 개요 응답을 이 순서로 먼저 낸다(재시도 확인용) — 비면 `outline`
        self.outline_queue: list[str] = []
        # 이 제목의 장은 늘 형식이 틀린 응답을 낸다(재시도까지 실패 → loop가 건너뛴다)
        self.broken_titles: set[str] = set()
        # 소스 요약 생성이 받은 1차 모델(#239) — `digest_calls`와 같은 순서. `models`(id → 모델)에
        # 있으면 그 모델이 응답한 것으로 결과에 싣는다 — 캐시 행의 `model_id`·`model_name`이 된다
        self.digest_models: list[uuid.UUID | None] = []
        self.models: dict[uuid.UUID, Any] = {}

    @staticmethod
    def kind_of(prompt: str) -> str:
        for kind, head in (
            ("outline", OUTLINE_HEAD),
            ("write", WRITE_HEAD),
            ("illustrate", ILLUSTRATE_HEAD),
        ):
            if prompt.startswith(head):
                return kind
        raise AssertionError(f"모르는 프롬프트: {prompt[:80]}")

    def calls_of(self, kind: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == kind]

    def reply(self, kind: str, prompt: str) -> str:
        if kind == "outline":
            if self.outline_queue:
                return self.outline_queue.pop(0)
            return json.dumps(self.outline, ensure_ascii=False)
        if kind == "write":
            match = ITEM_RE.search(prompt)
            assert match, prompt
            item = json.loads(match.group(1))
            if item["title"] in self.broken_titles:
                return '{"title": "x", "slides": [{"layout": "bullets"}]}'
            return json.dumps(written_slide(item), ensure_ascii=False)
        # 덱의 heading을 순서대로 옮겨 쓴다(장 수가 덱과 같다 — 코어가 범위 밖 번호를 버린다)
        block = _BLOCK_RE.search(prompt)
        assert block, prompt
        _title, _, slides = block.group(1).partition("\n")
        headings = [{"heading": s["heading"]} for s in json.loads(slides)]
        return json.dumps(
            {"title": "커피 입문", "slides": headings, "images": self.images}, ensure_ascii=False
        )

    async def stream(self, db, model_id, messages, result, **kwargs):
        result.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        prompt = messages[-1]["content"]
        kind = self.kind_of(prompt)
        self.calls.append({"kind": kind, "prompt": prompt, "max_tokens": kwargs.get("max_tokens")})
        text = self.reply(kind, prompt)

        async def gen():
            yield text

        return gen()

    async def digest_stream(self, db, model_id, messages, result, **kwargs):
        result.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        body = messages[-1]["content"]
        title = re.search(r"문서 제목: (.*)", body)
        self.digest_calls.append(title.group(1) if title else "")
        self.digest_models.append(model_id)
        if model_id in self.models:
            result.model = SimpleNamespace(model=self.models[model_id])
        text = f"요약 — {title.group(1) if title else ''}"

        async def gen():
            yield text

        return gen()


class SlideSearch:
    """`retrieve_context` 대역 — 범위(`source_ids`)의 소스마다 발췌 하나."""

    def __init__(self, titles: dict[str, str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.titles = titles or {}

    async def retrieve(self, db, notebook_id, query, source_ids=None, top_k=None):
        from biznotes_core.services.rag_chat import Citation, RetrievalResult

        self.calls.append({"query": query, "source_ids": source_ids})
        citations = [
            Citation(
                n=1,
                source_id=str(sid),
                chunk_id=str(uuid.uuid4()),
                quote=f"{self.titles.get(str(sid), sid)} 발췌",
                source_title=self.titles.get(str(sid), "소스"),
            )
            for sid in (source_ids or [])
        ]
        return RetrievalResult(citations=citations, insufficient_context=not citations)
