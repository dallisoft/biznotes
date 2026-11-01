/**
 * 실행 로그의 노드 실패 코드(`ExecutionLog.error_code`) → 한국어 문구 사전.
 *
 * 코드 목록의 정본은 코어 `biznotes_core/services/agents/node_errors.py`(`NODE_ERROR_CODES` 8종, 추가만
 * 한다 — 코어 v0.19.0 #216, `context_length_exceeded`는 v0.25.0 #358, `llm_output_truncated`는 v0.29.0 #422)이고, 문구는 `docs/plans/core-v0.20.0-upgrade/plan.md` §5.5다. 키 집합이
 * 코어와 같은지는 백엔드 테스트 `apps/api/tests/test_web_node_error_codes_contract.py`가 `make ci`에서
 * 본다 — 코어가 코드를 더하면 거기서 실패한다. 모르는 코드·코드 없음(null·undefined)은 일반 문구
 * 「노드 실행에 실패했습니다」로 보인다.
 *
 * 문구에는 서버 값을 넣지 않는다. 코어가 관리자 설정 원문(모델·공급자 이름, 공급자 응답)을 실행 로그
 * `error_message`에서 이미 고정 문구로 바꾸지만(`node_log_message`), 이 사전은 코드만 본다.
 */

export const NODE_ERROR_FALLBACK_LABEL = "노드 실행에 실패했습니다";

const MESSAGES: Record<string, string> = {
  rerank_unavailable: "리랭커를 사용할 수 없어 검색을 진행하지 못했습니다",
  model_call_failed: "모델 호출에 실패했습니다",
  gateway_error: "모델 설정이나 입력을 확인해야 합니다",
  ssrf_blocked: "내부 주소로의 요청이 차단되었습니다",
  tool_unavailable: "도구를 사용할 수 없습니다",
  usage_limit_exceeded: "사용량 한도를 넘었습니다",
  // 시도한 후보 모델이 모두 컨텍스트 초과로 실패했다(코어 v0.25.0 #358, #215) — 소스를 줄이라는 안내다
  context_length_exceeded: "입력이 모델 컨텍스트 한도를 넘었습니다",
  // 출력이 노드 max_tokens에서 잘려 JSON 검증을 재시도 없이 끝냈다(코어 v0.29.0 #422) — 상한을 올리거나
  // 입력을 줄이라는 안내다
  llm_output_truncated: "모델 출력이 길이 상한에서 잘렸습니다",
};

/** 코어가 정한 노드 실패 코드 8종(문구 사전의 키). */
export const KNOWN_NODE_ERROR_CODES: readonly string[] = Object.keys(MESSAGES);

/** 노드 실패 코드의 문구. 모르는 코드·null·undefined는 일반 문구다(자체 속성만 본다). */
export function nodeErrorLabel(code: string | null | undefined): string {
  if (typeof code !== "string" || !Object.prototype.hasOwnProperty.call(MESSAGES, code)) {
    return NODE_ERROR_FALLBACK_LABEL;
  }
  return MESSAGES[code];
}

/** 폴백 체인이 서킷이 열린 후보를 건너뛸 때의 코드 — 노드 실패 코드가 아니라 시도한 모델에만 온다(코어 #359). */
const CIRCUIT_OPEN_CODE = "circuit_open";

/**
 * 시도한 모델 하나의 실패 문구(`ExecutionLog.model_attempts[].error_code`, 제품 #216). 노드 실패 코드 문구에 더해
 * `circuit_open`(연속 실패로 잠시 건너뜀)을 구분한다. 그 밖의 모르는 코드는 일반 문구다.
 */
export function modelAttemptLabel(code: string | null | undefined): string {
  return code === CIRCUIT_OPEN_CODE ? "연속 실패로 잠시 건너뛰었습니다" : nodeErrorLabel(code);
}
