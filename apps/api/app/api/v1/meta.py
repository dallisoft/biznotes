"""앱 버전 조회 — 사이드바 브랜드 행 아래에 작게 표기한다.

설치된 배포판 메타데이터에서 읽는다. 출처는 `pyproject.toml` `[project].version`
하나이고(OpenAPI `info.version`도 같은 값), 코어는 같은 저장소에 있어 따로 표기하지 않는다.
로그인 사용자에게만 보인다 — 익명 요청에 버전(지문)을 내주지 않는다.
"""

from importlib.metadata import version

from biznotes_core.api.deps import get_current_user
from biznotes_core.models import User
from fastapi import APIRouter, Depends

from app.schemas.meta import VersionsOut

APP_VERSION = version("biznotes-api")

router = APIRouter(tags=["meta"])


@router.get("/meta/versions", response_model=VersionsOut)
async def get_versions(_user: User = Depends(get_current_user)) -> VersionsOut:
    return VersionsOut(app_version=APP_VERSION)
