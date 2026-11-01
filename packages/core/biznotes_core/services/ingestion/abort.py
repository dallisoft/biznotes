"""파싱 스레드에 협조적 중단 신호를 전달하는 ContextVar (계획 A §3.1.2·§5-7).

워커 `_run`이 heartbeat 핸들의 abort 이벤트를 여기에 실은 뒤 `anyio.to_thread.run_sync`를
호출한다. anyio 워커 스레드는 호출 시점의 contextvars 사본에서 돌므로 `hwp.convert()`가
시그니처 변경 없이 같은 Event를 본다. HWP 파서만 읽는다 — 다른 파서는 끝까지 돈다.
"""

import threading
from contextvars import ContextVar

PARSE_ABORT: ContextVar[threading.Event | None] = ContextVar("parse_abort", default=None)
