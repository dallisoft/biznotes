from biznotes_core.api.v1.auth import router as auth_router
from biznotes_core.api.v1.favorites import router as favorites_router
from biznotes_core.api.v1.groups import router as groups_router
from biznotes_core.api.v1.notebooks import router as notebooks_router
from biznotes_core.api.v1.prompts import router as prompts_router
from biznotes_core.api.v1.search import router as search_router
from biznotes_core.api.v1.skills import router as skills_router
from biznotes_core.api.v1.sources import router as sources_router
from biznotes_core.api.v1.tags import router as tags_router
from fastapi import APIRouter

from app.api.v1.admin import admin_router
from app.api.v1.agents import router as agents_router
from app.api.v1.chat import router as chat_router
from app.api.v1.meta import router as meta_router
from app.api.v1.notes import router as notes_router
from app.api.v1.run_logs import router as run_logs_router
from app.api.v1.skill_installs import router as skill_installs_router
from app.api.v1.workflows import router as workflows_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(notebooks_router)
api_router.include_router(sources_router)
api_router.include_router(notes_router)
api_router.include_router(tags_router)
api_router.include_router(favorites_router)
api_router.include_router(search_router)
api_router.include_router(chat_router)
api_router.include_router(agents_router)
api_router.include_router(run_logs_router)
api_router.include_router(skills_router)
# GitHub 저장소 스킬 설치 `/skill-installs/github`(#259) — 채팅 확인 카드의 버튼만 부른다.
# 소유자는 항상 요청자다(설계 노트 skill-install-github §4 권한 표)
api_router.include_router(skill_installs_router)
api_router.include_router(workflows_router)
api_router.include_router(prompts_router)
# 사용자 소유 자동화 `/me/agent-*`(코어 v0.17.0 #255) — 조립이 곧 노출이다. 무인 진입
# 전부가 코어 관문을 지나고(HTTP 웹훅 BL-216) 워커가 실행 직전 노트북 권한을 다시 본
# (BL-217) 뒤에만 연다(core-v0.17.1-upgrade D-4)
# 내 그룹 `/me/groups`(코어 v0.19.0 #278 1단계) — 요청자 자신의 소속만 읽는다(admin도 같다).
# 그룹 공유 대상 선택용이고 생성·구성원 관리는 관리자 API다(core-v0.20.0-upgrade D-10)
api_router.include_router(groups_router)
api_router.include_router(admin_router)
# 앱·코어 버전 `/meta/versions` — 사이드바 브랜드 행 아래 표기(로그인 사용자만)
api_router.include_router(meta_router)


@api_router.get("/ping", tags=["meta"])
async def ping() -> dict[str, str]:
    """FE↔BE 연결 확인용 (Phase 0)."""
    return {"message": "pong", "service": "biznotes-api"}
