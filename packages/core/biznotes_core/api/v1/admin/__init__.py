"""코어가 소유한 어드민 라우터.

조립(`admin_router`)은 프로젝트가 한다 — 제품마다 노출하는 어드민 화면이 다르고,
아직 코어로 오지 않은 라우터(agents·mcp)가 프로젝트에 남아 있기 때문이다.
코어가 조립까지 맡으면 프로젝트 라우터를 import하게 되어 경계가 깨진다.

**순서도 프로젝트가 정한다.** FastAPI는 등록 순서대로 OpenAPI 경로를 나열하므로,
코어 라우터를 한 덩어리로 먼저 넣으면 생성 타입에 불필요한 diff가 생긴다.
개별 라우터를 그대로 내보내 기존 순서를 그대로 재현할 수 있게 한다.
"""

from biznotes_core.api.v1.admin.audit import router as audit_router
from biznotes_core.api.v1.admin.limits import router as limits_router
from biznotes_core.api.v1.admin.llm import router as llm_router
from biznotes_core.api.v1.admin.rag import router as rag_router
from biznotes_core.api.v1.admin.skills import router as skills_router
from biznotes_core.api.v1.admin.source_digest import router as source_digest_router
from biznotes_core.api.v1.admin.users import router as users_router

__all__ = [
    "audit_router",
    "limits_router",
    "llm_router",
    "rag_router",
    "skills_router",
    "source_digest_router",
    "users_router",
]
