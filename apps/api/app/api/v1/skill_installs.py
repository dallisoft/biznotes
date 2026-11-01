"""GitHub 저장소 스킬 설치 API(#259, 설계 노트 docs/plans/skill-install-github.md §4).

채팅 확인 카드의 「설치」 버튼만 부른다 — 채팅 에이전트에는 이 경로를 부르는 도구가 없고
(`CHAT_SAFE_TOOLS` 그대로), 에이전트 워커의 HTTP는 받아오기 전용이라 POST를 보낼 수 없다.

권한: 로그인 사용자 누구나(노트북과 무관). 소유자는 항상 요청자이고(`user_id` 인자 없음),
다시 설치할 대상은 요청자 본인의 가져온 스킬에서만 찾는다. admin도 같다.
"""

from biznotes_core.api.deps import get_current_user
from biznotes_core.core.db import get_db
from biznotes_core.core.rate_limit import RateLimiter
from biznotes_core.models import User
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.skill_install import SkillInstallGithubOut, SkillInstallGithubRequest
from app.services.skills.github_install import install_from_github

router = APIRouter(prefix="/skill-installs", tags=["skills"])


@router.post(
    "/github",
    response_model=SkillInstallGithubOut,
    # 사용자 단위 한도 — 외부 URL을 받아 오는 소스 등록과 같은 기존 설정 키를 쓴다(새 env 키 없음)
    dependencies=[
        Depends(
            RateLimiter("skill_install", scope="user", limit_setting="rate_limit_source_create")
        )
    ],
    responses={
        422: {"description": "고른 경로를 하나도 설치하지 못했다(`detail.items`에 경로별 사유)"}
    },
)
async def install_github_skills(
    body: SkillInstallGithubRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SkillInstallGithubOut:
    """확인 카드에서 고른 경로를 요청자 private 스킬로 설치하고 표준 version을 동결한다.

    경로별로 커밋하고 경로별 결과(`installed`·`reinstalled`·`failed`)를 돌려준다. 하나도 설치하지
    못하면 422이고 `detail`은 `{code, detail, items}`다.
    """
    result = await install_from_github(db, user, body)
    if all(item.status == "failed" for item in result.items):
        first = result.items[0]
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": first.reason,
                "detail": first.message,
                "items": [item.model_dump(mode="json") for item in result.items],
            },
        )
    return result
