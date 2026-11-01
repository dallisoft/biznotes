"""소스 요약(digest) 미리 만들기 예약 — Redis 토큰 디바운스(#378).

설정 `source_digest_on_ingest`를 켜면 색인 교체가 소스를 ready로 바꾼 뒤 `schedule_digest_refresh`가
새 토큰을 발급하고 생성 태스크(`GENERATE_SOURCE_DIGEST`)를 countdown 뒤로 발행한다. 태스크는
토큰이 최신일 때만 만든다 — 같은 소스를 짧게 여러 번 재인제스트해도 마지막 예약 하나만 LLM을
부른다. 태스크는 import하지 않고 이름으로 발행한다(`ARCHITECTURE.md` §2, `task_publish`).
"""

import uuid

import redis as redis_sync

from biznotes_core.config import get_settings
from biznotes_core.services.task_publish import publish_task
from biznotes_core.workers.task_names import GENERATE_SOURCE_DIGEST

DEBOUNCE_SECONDS = 10
# 디바운스 토큰 키 — 태스크 본문이 같은 키로 최신 여부를 판정한다. 지우지 않고 만료에 맡긴다
TOKEN_KEY = "source_digest:debounce:{source_id}"
TOKEN_TTL_SECONDS = 3600


def schedule_digest_refresh(source_id: str, *, countdown: int = DEBOUNCE_SECONDS) -> None:
    """요약 생성 태스크를 예약한다 — 토큰을 갱신해 그 소스의 앞선 예약을 무효로 만든다."""
    token = uuid.uuid4().hex
    client = redis_sync.Redis.from_url(get_settings().redis_url)
    try:
        client.set(TOKEN_KEY.format(source_id=source_id), token, ex=TOKEN_TTL_SECONDS)
    finally:
        client.close()
    publish_task(GENERATE_SOURCE_DIGEST, args=[source_id, token], countdown=countdown)
