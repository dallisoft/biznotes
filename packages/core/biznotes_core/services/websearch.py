"""웹 검색 — SearxNG (기본) 또는 Tavily (API 키 설정 시)."""

from typing import Any

import httpx

from biznotes_core.config import get_settings


class SearchProviderError(Exception):
    pass


async def _searxng_search(query: str) -> list[dict[str, Any]]:
    params: dict[str, str] = {"q": query, "format": "json"}
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(f"{get_settings().searxng_url}/search", params=params)
        if res.status_code != 200:
            raise SearchProviderError(f"SearxNG 응답 오류: HTTP {res.status_code}")
        data = res.json()
    return [
        {
            "title": r.get("title") or r.get("url", ""),
            "url": r["url"],
            "snippet": r.get("content"),
            "engine": r.get("engine"),
        }
        for r in data.get("results", [])
        if r.get("url")
    ][:20]


async def _tavily_search(query: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": get_settings().tavily_api_key, "query": query, "max_results": 10},
        )
        if res.status_code != 200:
            raise SearchProviderError(f"Tavily 응답 오류: HTTP {res.status_code}")
        data = res.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r["url"],
            "snippet": r.get("content"),
            "engine": "tavily",
        }
        for r in data.get("results", [])
        if r.get("url")
    ]


async def web_search(query: str) -> list[dict[str, Any]]:
    s = get_settings()
    if s.search_provider == "tavily" and s.tavily_api_key:
        return await _tavily_search(query)
    return await _searxng_search(query)
