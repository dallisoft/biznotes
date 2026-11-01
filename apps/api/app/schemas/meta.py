from pydantic import BaseModel, Field


class VersionsOut(BaseModel):
    """사이드바 브랜드 행 아래에 표기하는 앱 버전(`v` 접두사 없는 PEP 440 문자열)."""

    app_version: str = Field(description="biznotes 버전 — pyproject `[project].version`")
