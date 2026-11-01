import uuid

from pydantic import BaseModel, ConfigDict


class MyGroupOut(BaseModel):
    """요청자가 속한 그룹 — 노트북 그룹 공유의 대상 선택용(#278)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
