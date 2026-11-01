"""노트북 개요 재생성 예약 — Redis 토큰 디바운스 (#321).

소스 ready 전이마다 `schedule_overview_refresh`가 새 토큰을 발급하고, countdown 뒤 실행되는
태스크(`GENERATE_NOTEBOOK_OVERVIEW`)는 토큰이 최신일 때만 생성한다(연속 인제스천 시 마지막 1회만
LLM 호출). 라우터와 워커가 함께 쓰므로 서비스 계층에 둔다. 태스크는 import하지 않고 이름으로
발행한다(`ARCHITECTURE.md` §2, `task_publish`). `biznotes_core.workers.tasks.overview`가 같은 객체를
재노출한다.
"""

import uuid

import redis as redis_sync

from biznotes_core.config import get_settings
from biznotes_core.services.task_publish import publish_task
from biznotes_core.workers.task_names import GENERATE_NOTEBOOK_OVERVIEW

DEBOUNCE_SECONDS = 10
# 디바운스 토큰 키 — 태스크 본문이 같은 키로 최신 여부를 판정한다
TOKEN_KEY = "overview:debounce:{notebook_id}"


def schedule_overview_refresh(
    notebook_id: str, *, debounce: bool = True, countdown: int = DEBOUNCE_SECONDS
) -> None:
    """재생성 태스크 예약.

    debounce=True  (인제스천 경로): 토큰을 갱신해 기존 예약을 무효화 — 마지막 1회만 실행.
    debounce=False (조회 경로): 대기 중 예약이 있으면 아무것도 하지 않음 (SET NX) —
                   폴링이 토큰을 계속 갱신해 태스크를 영원히 굶기는 것을 방지.
    countdown=0    (수동 재생성): 디바운스 대기 없이 즉시 실행.
    """
    token = uuid.uuid4().hex
    key = TOKEN_KEY.format(notebook_id=notebook_id)
    client = redis_sync.Redis.from_url(get_settings().redis_url)
    try:
        if debounce:
            client.set(key, token, ex=3600)
        else:
            if not client.set(key, token, ex=3600, nx=True):
                return  # 이미 예약됨
    finally:
        client.close()
    publish_task(GENERATE_NOTEBOOK_OVERVIEW, args=[notebook_id, token], countdown=countdown)
