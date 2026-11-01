"""앱 레벨 Prometheus 메트릭 — /metrics로 노출 (기본 레지스트리).

HTTP 메트릭은 prometheus-fastapi-instrumentator가 자동 수집하고,
여기는 LLM 게이트웨이 등 도메인 메트릭만 정의한다.
주의: Celery 워커 프로세스의 카운터는 API의 /metrics에 노출되지 않는다
(워커 메트릭은 백로그 — exporter 별도 필요).
"""

from prometheus_client import Counter, Histogram

llm_fallback_total = Counter(
    "llm_fallback_total",
    "1차 모델 실패로 폴백 후보가 응답한 횟수",
    ["modality"],
)

llm_provider_error_total = Counter(
    "llm_provider_error_total",
    "LLM 후보 호출 실패 횟수 (재시도 소진 기준)",
    ["provider"],
)

llm_circuit_open_total = Counter(
    "llm_circuit_open_total",
    "서킷 브레이커가 새로 열린 횟수",
    ["provider"],
)

citation_verified_total = Counter(
    "citation_verified_total",
    "인용-청크 매칭 검증 결과 (unverified 비율 상시 관측용)",
    ["result"],  # verified | unverified
)

# 임베딩 동시성 게이트(#196). 인제스트(워커)와 질의(API) **양쪽** 경로에서 증가하므로
# API /metrics에도 오른다. 다만 워커 프로세스의 카운터만은 이 프로세스에 없으므로
# `GET /admin/llm/providers/{id}/embedding-gate`가 리스 수와 마지막 오류를 직접 준다(구현 리뷰 m2)
embedding_gate_total = Counter(
    "embedding_gate_total",
    "임베딩 동시성 게이트 시도 결과",
    ["result"],  # acquired | bypass | timeout | redis_error
)

embedding_gate_wait_seconds = Histogram(
    "embedding_gate_wait_seconds",
    "임베딩 슬롯을 잡는 데 실제로 쓴 대기 시간(초). 상한 없음이면 관측되지 않는다",
)

# 에이전트 외부 HTTP 연결 정책(#395)이 거부한 요청.
# 사유는 `core.ssrf.EgressBlockedError.reason`이다. 거부는 주로 에이전트 워커에서
# 일어나므로 API /metrics에는 대개 오르지 않는다(위 주의)
agent_http_blocked_total = Counter(
    "agent_http_blocked_total",
    "에이전트 외부 HTTP 연결 정책이 거부한 요청 수",
    ["reason"],
)
