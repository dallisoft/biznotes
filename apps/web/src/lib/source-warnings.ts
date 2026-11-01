import type { components } from "@biznotes/shared-types";
import { formatNumber, formatPercent } from "@/lib/format";

/**
 * 소스 인제스트 경고(`SourceOut.warnings`) → 한국어 문구 사전.
 *
 * 코드 목록의 정본은 코어 `biznotes_core/services/ingestion/source_warnings.py`(`WARNING_CODES` 15종,
 * 추가만 한다)이고, 문구 기준은 코어 `docs/plans/source-metadata/plan.md`의 표다. 키 집합이 코어와
 * 같은지는 백엔드 테스트 `apps/api/tests/test_web_source_warnings_contract.py`가 `make ci`에서 본다.
 * 모르는 코드는 일반 경고 「알 수 없는 인제스트 경고(code)」로 보인다. `params` 값은 int·float·str
 * 중 하나이며(코어 계약) 문자열 값은 문서에서 온 외부 문자열일 수 있으므로 React 텍스트로만
 * 렌더한다(T-6). 보안 코드 4종(코어 `SECURITY_CODES` — `threat_findings`·`hidden_text`·
 * `injection_signals`·`threat_scan_records_skipped`)은 주의 스타일로 구분하고, 집합이 코어와 같은지도
 * 같은 백엔드 테스트가 본다.
 *
 * `threat_scan_records_skipped`(코어 v0.20.0 #231)는 탐지 결과가 아니라 검사 범위가 줄었다는 안내다 —
 * 배포용(보호) HWP의 본문 레코드 검사를 건너뛰었다. 그래서 문구는 「검사를 건너뛰었다」로 쓴다.
 *
 * 재인덱싱 안내 2종(`reindex_kept_previous`·`reindex_deferred`, 코어 v0.16.0 #230)은 원본 파일의
 * 성질이 아니라 색인 단계가 `ready` 행에 쓰고 새 색인이 커밋되면 지운다. 코어 문구 표가 없어 제품이
 * 문구를 정하고 사유 코드는 한국어 라벨로 보인다(모르는 사유는 원문 텍스트).
 */

export type SourceWarning = components["schemas"]["SourceWarning"];
type Params = NonNullable<SourceWarning["params"]>;

export const SECURITY_WARNING_CODES: ReadonlySet<string> = new Set([
  "threat_findings",
  "hidden_text",
  "injection_signals",
  "threat_scan_records_skipped",
]);

const LEVEL_LABEL: Record<string, string> = {
  low: "낮음",
  medium: "중간",
  high: "높음",
  other: "기타",
};

const OCR_STOP_LABEL: Record<string, string> = {
  page_cap: "쪽 수 상한",
  deadline: "시간 상한",
  char_budget: "글자 예산",
};

const REINDEX_REASON_LABEL: Record<string, string> = {
  parse: "원본 파싱 실패",
  embedding_transient: "임베딩 서버 일시 오류",
  embedding_gate: "임베딩 동시성 상한",
  error: "처리 오류",
};

function int(params: Params, key: string): string {
  const v = params[key];
  return typeof v === "number" && Number.isFinite(v) ? formatNumber(Math.trunc(v)) : "?";
}

function percent(params: Params, key: string): string {
  const v = params[key];
  return typeof v === "number" && Number.isFinite(v) ? formatPercent(v, 1) : "?";
}

function label(params: Params, key: string, labels: Record<string, string>): string {
  const v = params[key];
  if (typeof v !== "string") return "?";
  return labels[v] ?? v;
}

const MESSAGES: Record<string, (p: Params) => string> = {
  text_truncated: (p) => `본문이 처리 상한에 닿아 일부가 잘렸습니다(생략 ${int(p, "omitted")}개).`,
  table_appendix_truncated: (p) => `표 부록 일부가 잘렸습니다(생략 ${int(p, "omitted")}개).`,
  nested_table_flattened: () => "중첩된 표를 평탄화했습니다 — 표 구조가 원본과 다를 수 있습니다.",
  deceptive_codepoints_removed: (p) =>
    `눈속임 문자(양방향 제어 등) ${int(p, "count")}개를 지웠습니다.`,
  spreadsheet_rows_truncated: (p) =>
    `${int(p, "sheet_index")}번째 시트가 행 상한(${int(p, "max_rows")}행)에 닿아 잘렸을 수 있습니다.`,
  threat_findings: (p) =>
    `거부 기준 미만의 위협 신호가 있습니다(${int(p, "count")}건, 등급 ${label(p, "severity", LEVEL_LABEL)}).`,
  hidden_text: (p) =>
    `원본 문서에 숨은 글자가 있습니다(${int(p, "chars")}자, 비율 ${percent(p, "ratio")}).`,
  injection_signals: (p) =>
    `프롬프트 인젝션으로 보이는 문구가 있습니다(${int(p, "count")}건, 신뢰도 ${label(p, "confidence", LEVEL_LABEL)}).`,
  threat_scan_records_skipped: () =>
    "배포용(보호) 문서라 보안 검사 중 본문 레코드 검사를 건너뛰었습니다. 첨부 개체(BinData·OLE) 검사는 했습니다.",
  structure_unavailable: () =>
    "HWP 구조 사본을 만들지 못했습니다 — 본문은 정상이고 구조 도구(hwp_outline·hwp_fields)만 쓸 수 없습니다.",
  ocr_pages: (p) => `그림만 있는 쪽 ${int(p, "count")}개를 OCR로 채웠습니다.`,
  ocr_pages_truncated: (p) =>
    `쪽 OCR이 ${label(p, "reason", OCR_STOP_LABEL)}에 닿아 멈췄습니다(${int(p, "done")}/${int(p, "candidates")}쪽).`,
  ocr_page_failed: (p) => `쪽 ${int(p, "count")}개의 OCR에 실패해 건너뛰었습니다.`,
  reindex_kept_previous: (p) =>
    `재인덱싱에 실패해 이전 색인을 그대로 씁니다(사유: ${label(p, "reason", REINDEX_REASON_LABEL)}).`,
  reindex_deferred: (p) =>
    `재인덱싱이 미뤄졌습니다(${label(p, "reason", REINDEX_REASON_LABEL)}) — 약 ${int(p, "retry_in_seconds")}초 뒤 다시 시도합니다(${int(p, "attempt")}/${int(p, "max_attempts")}회). 그동안 이전 색인으로 검색됩니다.`,
};

/** 코어가 정한 경고 코드 15종(문구 사전의 키). */
export const KNOWN_WARNING_CODES: readonly string[] = Object.keys(MESSAGES);

export interface SourceWarningView {
  code: string;
  message: string;
  security: boolean;
  known: boolean;
}

export function describeSourceWarning(warning: SourceWarning): SourceWarningView {
  const code = String(warning.code);
  const params: Params = warning.params ?? {};
  const make = Object.prototype.hasOwnProperty.call(MESSAGES, code) ? MESSAGES[code] : undefined;
  return {
    code,
    message: make ? make(params) : `알 수 없는 인제스트 경고(${code})`,
    security: SECURITY_WARNING_CODES.has(code),
    known: make !== undefined,
  };
}

/** `null`(기록 없음)·`[]`(경고 없음)은 표시할 것이 없다. */
export function describeSourceWarnings(
  warnings: SourceWarning[] | null | undefined,
): SourceWarningView[] {
  return (warnings ?? []).map(describeSourceWarning);
}
