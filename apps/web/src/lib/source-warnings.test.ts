import { describe, expect, it } from "vitest";
import {
  describeSourceWarning,
  describeSourceWarnings,
  KNOWN_WARNING_CODES,
  SECURITY_WARNING_CODES,
} from "@/lib/source-warnings";

// 코어 biznotes_core/services/ingestion/source_warnings.py WARNING_CODES(v0.20.0) — 추가만 하는 목록.
// 코어와의 대조(코드 집합·보안 집합)는 백엔드 apps/api/tests/test_web_source_warnings_contract.py가 한다.
const CORE_CODES = [
  "text_truncated",
  "table_appendix_truncated",
  "nested_table_flattened",
  "deceptive_codepoints_removed",
  "spreadsheet_rows_truncated",
  "threat_findings",
  "hidden_text",
  "injection_signals",
  "threat_scan_records_skipped",
  "structure_unavailable",
  "ocr_pages",
  "ocr_pages_truncated",
  "ocr_page_failed",
  "reindex_kept_previous",
  "reindex_deferred",
];

describe("source-warnings — 코드 사전 (v7 AC-25 승계, AC-3.6)", () => {
  it("코어 경고 코드 15종 모두에 문구가 있다", () => {
    expect([...KNOWN_WARNING_CODES].sort()).toEqual([...CORE_CODES].sort());
    for (const code of CORE_CODES) {
      const view = describeSourceWarning({ code, params: {} });
      expect(view.known).toBe(true);
      expect(view.message).not.toContain("알 수 없는");
    }
  });

  it.each([
    [{ code: "text_truncated", params: { omitted: 1234 } }, "본문이 처리 상한에 닿아 일부가 잘렸습니다(생략 1,234개)."],
    [{ code: "table_appendix_truncated", params: { omitted: 3 } }, "표 부록 일부가 잘렸습니다(생략 3개)."],
    [{ code: "deceptive_codepoints_removed", params: { count: 2 } }, "눈속임 문자(양방향 제어 등) 2개를 지웠습니다."],
    [{ code: "spreadsheet_rows_truncated", params: { sheet_index: 2, max_rows: 2000 } }, "2번째 시트가 행 상한(2,000행)에 닿아 잘렸을 수 있습니다."],
    [{ code: "threat_findings", params: { severity: "medium", count: 4 } }, "거부 기준 미만의 위협 신호가 있습니다(4건, 등급 중간)."],
    [{ code: "hidden_text", params: { chars: 120, ratio: 0.0345 } }, "원본 문서에 숨은 글자가 있습니다(120자, 비율 3.5%)."],
    [{ code: "injection_signals", params: { count: 1, confidence: "high" } }, "프롬프트 인젝션으로 보이는 문구가 있습니다(1건, 신뢰도 높음)."],
    [
      { code: "threat_scan_records_skipped" },
      "배포용(보호) 문서라 보안 검사 중 본문 레코드 검사를 건너뛰었습니다. 첨부 개체(BinData·OLE) 검사는 했습니다.",
    ],
    [{ code: "ocr_pages", params: { count: 5 } }, "그림만 있는 쪽 5개를 OCR로 채웠습니다."],
    [{ code: "ocr_pages_truncated", params: { done: 3, candidates: 9, reason: "deadline" } }, "쪽 OCR이 시간 상한에 닿아 멈췄습니다(3/9쪽)."],
    [{ code: "ocr_page_failed", params: { count: 1 } }, "쪽 1개의 OCR에 실패해 건너뛰었습니다."],
    [{ code: "reindex_kept_previous", params: { reason: "parse" } }, "재인덱싱에 실패해 이전 색인을 그대로 씁니다(사유: 원본 파싱 실패)."],
    [{ code: "reindex_kept_previous", params: { reason: "embedding_transient" } }, "재인덱싱에 실패해 이전 색인을 그대로 씁니다(사유: 임베딩 서버 일시 오류)."],
    [{ code: "reindex_kept_previous", params: { reason: "embedding_gate" } }, "재인덱싱에 실패해 이전 색인을 그대로 씁니다(사유: 임베딩 동시성 상한)."],
    [{ code: "reindex_kept_previous", params: { reason: "error" } }, "재인덱싱에 실패해 이전 색인을 그대로 씁니다(사유: 처리 오류)."],
    [
      { code: "reindex_deferred", params: { reason: "embedding_transient", attempt: 2, max_attempts: 3, retry_in_seconds: 120 } },
      "재인덱싱이 미뤄졌습니다(임베딩 서버 일시 오류) — 약 120초 뒤 다시 시도합니다(2/3회). 그동안 이전 색인으로 검색됩니다.",
    ],
    [
      { code: "reindex_deferred", params: { reason: "embedding_gate", attempt: 10, max_attempts: 10, retry_in_seconds: 1200 } },
      "재인덱싱이 미뤄졌습니다(임베딩 동시성 상한) — 약 1,200초 뒤 다시 시도합니다(10/10회). 그동안 이전 색인으로 검색됩니다.",
    ],
  ])("params 서식: %j", (warning, message) => {
    expect(describeSourceWarning(warning).message).toBe(message);
  });

  it("params가 없거나 형식이 다르면 ?로 채우고 깨지지 않는다", () => {
    expect(describeSourceWarning({ code: "text_truncated" }).message).toContain("생략 ?개");
    expect(describeSourceWarning({ code: "hidden_text", params: { chars: "x", ratio: "y" } }).message).toBe(
      "원본 문서에 숨은 글자가 있습니다(?자, 비율 ?).",
    );
    expect(describeSourceWarning({ code: "reindex_kept_previous" }).message).toBe(
      "재인덱싱에 실패해 이전 색인을 그대로 씁니다(사유: ?).",
    );
    expect(
      describeSourceWarning({ code: "reindex_deferred", params: { reason: 1, attempt: "x", retry_in_seconds: "soon" } }).message,
    ).toBe("재인덱싱이 미뤄졌습니다(?) — 약 ?초 뒤 다시 시도합니다(?/?회). 그동안 이전 색인으로 검색됩니다.");
  });

  it("모르는 등급·사유 문자열은 원문을 그대로 쓴다(텍스트 렌더)", () => {
    expect(describeSourceWarning({ code: "threat_findings", params: { severity: "<b>x</b>", count: 1 } }).message).toContain(
      "등급 <b>x</b>",
    );
    expect(describeSourceWarning({ code: "ocr_pages_truncated", params: { reason: "new_reason", done: 1, candidates: 2 } }).message).toContain(
      "new_reason",
    );
    expect(describeSourceWarning({ code: "reindex_kept_previous", params: { reason: "<i>future</i>" } }).message).toBe(
      "재인덱싱에 실패해 이전 색인을 그대로 씁니다(사유: <i>future</i>).",
    );
    expect(
      describeSourceWarning({ code: "reindex_deferred", params: { reason: "new_reason", attempt: 1, max_attempts: 3, retry_in_seconds: 60 } }).message,
    ).toContain("재인덱싱이 미뤄졌습니다(new_reason)");
  });

  it("재인덱싱 안내 2종은 보안 신호가 아니다(muted)", () => {
    for (const code of ["reindex_kept_previous", "reindex_deferred"]) {
      const view = describeSourceWarning({ code, params: { reason: "parse" } });
      expect(view.known).toBe(true);
      expect(view.security).toBe(false);
    }
  });

  it("모르는 코드는 「알 수 없는 인제스트 경고(code)」", () => {
    const view = describeSourceWarning({ code: "brand_new_code", params: { n: 1 } });
    expect(view).toEqual({
      code: "brand_new_code",
      message: "알 수 없는 인제스트 경고(brand_new_code)",
      security: false,
      known: false,
    });
    // 프로토타입 키도 모르는 코드다
    expect(describeSourceWarning({ code: "constructor" }).known).toBe(false);
  });

  it("보안 코드 4종만 security — 검사 범위 축소 안내(threat_scan_records_skipped) 포함(코어 SECURITY_CODES)", () => {
    expect([...SECURITY_WARNING_CODES].sort()).toEqual([
      "hidden_text",
      "injection_signals",
      "threat_findings",
      "threat_scan_records_skipped",
    ]);
    // 탐지 결과가 아니라 검사 범위 축소 안내다 — 문구가 탐지로 읽히지 않는다(코어 v0.20.0 #231)
    expect(describeSourceWarning({ code: "threat_scan_records_skipped" }).message).not.toMatch(/탐지|위협 신호/);
    for (const code of CORE_CODES) {
      expect(describeSourceWarning({ code }).security).toBe(SECURITY_WARNING_CODES.has(code));
    }
  });

  it("null·[]는 표시할 경고가 없다", () => {
    expect(describeSourceWarnings(null)).toEqual([]);
    expect(describeSourceWarnings(undefined)).toEqual([]);
    expect(describeSourceWarnings([])).toEqual([]);
    expect(describeSourceWarnings([{ code: "nested_table_flattened" }])).toHaveLength(1);
  });
});
