"""PATCH 본문 해석 — 생략한 필드와 명시적 null을 구분한다 (#258).

`model_dump(exclude_unset=True)`는 생략한 필드를 빼지만 명시적 `null`은 남긴다. 대응 열이
NOT NULL이면 그 `None`이 커밋 때 무결성 오류(500)가 된다. 사용자 자동화 경로(#255)에서 쓰던
규칙을 관리자 경로와 함께 쓴다.
"""

from collections.abc import Collection
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel

__all__ = ["non_null_changes"]


def non_null_changes(body: BaseModel, *, nullable: Collection[str] = ()) -> dict[str, Any]:
    """보낸 필드만 돌려준다. 명시적 null은 422다 — 생략(기존 값 유지)과 구분한다.

    NOT NULL 열에 대응하는 수정 스키마에 쓴다. null이 뜻을 갖는 필드(값 해제 등)는 `nullable`에
    적는다 — 그 필드의 null은 거부하지 않고 그대로 돌려준다(#267). 오류 문구에는 필드 이름만
    싣고 입력값은 싣지 않는다.
    `extra="allow"`·alias 스키마에는 쓰지 않는다 — 클라이언트가 보낸 키나 와이어 이름과 다른
    파이썬 필드 이름이 문구에 그대로 실린다.
    """
    changes = body.model_dump(exclude_unset=True)
    nulls = sorted(
        field for field, value in changes.items() if value is None and field not in nullable
    )
    if nulls:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"null로 바꿀 수 없는 필드: {', '.join(nulls)}"
        )
    return changes
