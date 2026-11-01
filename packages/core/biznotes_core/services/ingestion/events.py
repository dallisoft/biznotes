"""인제스트 파싱 단계 structlog 이벤트 이름 — 이름 문자열의 유일한 정의 위치.

접두는 `ingest.parse.*`이며, 이 모듈 밖에서는 이름 리터럴을 쓰지 않고 아래 상수만 import한다.
"""

INGEST_PARSE_ADMISSION_REJECTED = "ingest.parse.admission_rejected"
INGEST_PARSE_DONE = "ingest.parse.done"
INGEST_PARSE_REJECTED = "ingest.parse.rejected"
INGEST_PARSE_HEARTBEAT_FAILED = "ingest.parse.heartbeat_failed"
INGEST_PARSE_HEARTBEAT_LOST = "ingest.parse.heartbeat_lost"
