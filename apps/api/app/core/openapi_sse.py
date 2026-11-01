"""SSE 이벤트 페이로드 모델을 OpenAPI 컴포넌트로 내보낸다(#259).

채팅 SSE 이벤트는 HTTP 응답 스키마가 아니라 FastAPI가 OpenAPI에 넣지 않는다. 손으로 미러링한
웹 타입은 드리프트가 기계로 잡히지 않으므로, 이벤트 모델을 컴포넌트로 더해 `pnpm gen:types`가
생성 타입을 만들게 한다(계약의 정본은 Pydantic → OpenAPI, 루트 AGENTS.md 「공유 계약」).
"""

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema


def add_sse_event_schemas(app: FastAPI, models: Sequence[type[BaseModel]]) -> None:
    """`app.openapi()` 결과의 `components.schemas`에 `models`(와 그 하위 모델)를 더한다.

    같은 이름의 다른 스키마가 이미 있으면 조용히 덮지 않고 실패한다.
    """
    base = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = base()
        _, top = models_json_schema(
            [(model, "serialization") for model in models],
            ref_template="#/components/schemas/{model}",
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for name, definition in top.get("$defs", {}).items():
            existing = components.get(name)
            if existing is not None and existing != definition:
                raise RuntimeError(f"OpenAPI 스키마 이름 충돌: {name}")
            components[name] = definition
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
