/**
 * 리랭커 설정값(`rag.rerank_model`) 형식 — 코어 v0.15.0 #209 `normalize_rerank_key`와 같은 규칙.
 *
 * - `gateway:<model_id>:<model_name>` — 등록된 게이트웨이 리랭커
 * - `fastembed:<model>` — 로컬 cross-encoder
 * - 접두사 없는 옛 값(예: `BAAI/bge-reranker-base`) — `fastembed:`로 본다(저장값은 바꾸지 않는다)
 * - `""` — 자동(활성 기본 리랭커, 없으면 로컬 기본)
 */
export const GATEWAY_RERANK_PREFIX = "gateway:";
export const FASTEMBED_RERANK_PREFIX = "fastembed:";

/**
 * 코어가 쓰는 Python `str.strip()`의 공백 — `str.isspace()`가 참인 29자.
 *
 * JavaScript `String.prototype.trim()`과 집합이 다르다(U+0085·U+001C~001F를 남기고 U+FEFF를 지운다).
 * 다르면 코어와 다른 키로 비교해 같은 항목을 다시 골라도 원값이 재기록된다(리뷰 P2-07).
 * Python 쪽과의 일치는 `apps/api/tests/test_web_modalities_contract.py`가 `make ci`에서 대조한다.
 */
export const PYTHON_WHITESPACE_CODE_POINTS = [
  0x0009, 0x000a, 0x000b, 0x000c, 0x000d, 0x001c, 0x001d, 0x001e, 0x001f, 0x0020, 0x0085, 0x00a0,
  0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
] as const;

const PYTHON_WHITESPACE = new Set<string>(
  PYTHON_WHITESPACE_CODE_POINTS.map((code) => String.fromCharCode(code)),
);

/** Python `str.strip()`과 같은 양끝 공백 제거(위 29자만, 모두 BMP라 UTF-16 한 단위). */
export function pythonStrip(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && PYTHON_WHITESPACE.has(value[start])) start += 1;
  while (end > start && PYTHON_WHITESPACE.has(value[end - 1])) end -= 1;
  return value.slice(start, end);
}

/** 저장값을 비교용 키로 바꾼다 — 화면 선택 표시에만 쓰고 저장값에는 쓰지 않는다(계획 D-4). */
export function normalizeRerankKey(value: string | null | undefined): string {
  const key = pythonStrip(value ?? "");
  if (!key || key.startsWith(GATEWAY_RERANK_PREFIX) || key.startsWith(FASTEMBED_RERANK_PREFIX)) {
    return key;
  }
  return `${FASTEMBED_RERANK_PREFIX}${key}`;
}
