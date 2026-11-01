"""에이전트 입력 계약 중 스킬 API가 함께 쓰는 부분 (표준 계획 D12).

두 제품의 `app.schemas.agent`에 같은 정의가 있었다. 스킬 라우터를 코어로 옮기면서
정본을 코어로 옮기고, 제품 스키마는 이 클래스를 재노출한다(OpenAPI 컴포넌트 이름 유지).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JsonSchemaProperty(BaseModel):
    """폼 파라미터 한 개의 JSON Schema 서술자."""

    model_config = ConfigDict(extra="allow")

    type: Literal["string", "integer", "number", "boolean"]
    title: str | None = None
    description: str | None = None
    enum: list[str | int | float | bool] | None = None
    default: str | int | float | bool | None = None
    minimum: float | None = None
    maximum: float | None = None
    minLength: int | None = None
    format: str | None = None


class ParamsSchema(BaseModel):
    """에이전트와 워크플로우가 수신하는 JSON Schema의 지원 범위."""

    model_config = ConfigDict(extra="allow")

    type: Literal["object"] = "object"
    properties: dict[str, JsonSchemaProperty] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)


class AgentInput(BaseModel):
    """에이전트 실행 시 입력 소스 선택 정책."""

    model_config = ConfigDict(extra="allow")

    mode: Literal["rag", "full", "none"] = "rag"
    scope: Literal["all", "selected"] = "all"
    max_sources: int | None = None
    max_chars: int | None = None
