from biznotes_core.api.deps import require_admin
from biznotes_core.api.v1.admin import (
    audit_router,
    llm_router,
    rag_router,
    skills_router,
    source_digest_router,
    users_router,
)
from biznotes_core.api.v1.admin import limits_router as core_limits_router
from fastapi import APIRouter, Depends

from app.api.v1.admin.agents import router as agents_router
from app.api.v1.admin.limits import router as limits_router
from app.api.v1.admin.mcp import router as mcp_router

admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)], tags=["admin"])
# 등록 순서는 이관 전과 동일하게 유지한다 — FastAPI가 이 순서로 OpenAPI 경로를
# 나열하므로, 바꾸면 생성 타입에 기능과 무관한 diff가 생긴다.
admin_router.include_router(users_router)
admin_router.include_router(llm_router)
admin_router.include_router(rag_router)
admin_router.include_router(agents_router)
admin_router.include_router(mcp_router)
admin_router.include_router(skills_router)
admin_router.include_router(audit_router)
# 「사용 제한」 — 제품 항목(`/admin/limits`, 채팅 1회 한도)과 코어 항목(코어 v0.26.0
# #386·#387·#388 — 레이트 리밋·일일 한도 기본값·업로드 최대 크기). 새 경로라 맨 끝에 둔다.
admin_router.include_router(limits_router)
admin_router.include_router(core_limits_router)
# 소스 요약 백필 `/admin/source-digests/backfill`(코어 v0.30.0 #454, #285) — 조립이 곧 노출이다.
# 코어가 경로마다 관리자를 다시 확인하고 감사 `source_digest.backfill`을 남긴다.
# 새 경로라 맨 끝이다.
admin_router.include_router(source_digest_router)
