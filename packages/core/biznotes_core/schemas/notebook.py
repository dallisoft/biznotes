import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from biznotes_core.schemas.enums import NotebookVisibility, SharePermission

_VISIBILITY = "`private`·`public`(배포 안의 모든 로그인 사용자에게 읽기)"


class NotebookCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    emoji: str = Field(default="📓", max_length=16)
    description: str = Field(default="", max_length=2000)


class NotebookUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    emoji: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=2000)
    visibility: NotebookVisibility | None = Field(
        default=None,
        description=_VISIBILITY + ". 값을 바꾸는 것은 소유자 권한(실제 소유자·admin)만 할 수 있다",
    )


class NotebookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    emoji: str
    description: str
    visibility: NotebookVisibility = Field(description=_VISIBILITY)
    created_at: datetime
    updated_at: datetime
    # Phase 2에서 sources 테이블 생성 후 실제 카운트로 대체
    source_count: int = 0
    is_owner: bool = Field(
        default=False, description="요청자가 실제 소유자인지. admin 승격과 무관하다"
    )
    permission: Literal["owner", "write", "read"] = Field(
        default="read",
        description=(
            "요청자의 유효 권한. 목록과 단건이 같은 규칙이다 — admin은 소유자가 아니어도 owner다"
        ),
    )
    tags: list[str] = Field(default_factory=list)
    is_favorite: bool = False


class NotebookOverviewOut(BaseModel):
    summary: str | None = None
    suggested_questions: list[str] = Field(default_factory=list)
    title_suggestions: list[str] = Field(default_factory=list)
    generated_at: datetime | None = None
    source_count: int = Field(
        default=0,
        description=(
            "개요 생성 입력에 들어간 ready 소스 수(#449). 개요는 소스를 빼지 않으므로 생성 시점의 "
            "ready 소스 전체 수와 같다. 생성된 개요가 없으면 0"
        ),
    )
    stale: bool = True


class NotebookShareRequest(BaseModel):
    # 모르는 필드는 422다 — 없앤 사용자 대상(`user_id`·`user_email`)을 조용히 무시하지 않는다(#278)
    model_config = ConfigDict(extra="forbid")

    group_id: uuid.UUID = Field(description="노트북 실제 소유자가 속한 그룹(`GET /me/groups`)")
    permission: SharePermission = "read"
    mode: Literal["set", "upgrade_only"] = Field(
        default="set",
        description=(
            "같은 대상이 이미 있을 때의 권한 갱신 방식(#300). `set`은 요청 권한으로 바꾼다"
            "(하향 포함). `upgrade_only`는 잠금 안에서 기존 권한과 요청 중 높은 쪽을 남긴다 — "
            "결정(GET)과 쓰기 사이에 다른 요청이 올린 권한을 낮추지 않는다. 결과 권한은 응답 "
            "`permission`이다"
        ),
    )


class NotebookShareOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notebook_id: uuid.UUID
    group_id: uuid.UUID
    permission: SharePermission
