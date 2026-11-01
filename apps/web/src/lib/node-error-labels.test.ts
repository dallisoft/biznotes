import { describe, expect, it } from "vitest";
import {
  KNOWN_NODE_ERROR_CODES,
  modelAttemptLabel,
  NODE_ERROR_FALLBACK_LABEL,
  nodeErrorLabel,
} from "@/lib/node-error-labels";

// 코어 biznotes_core/services/agents/node_errors.py NODE_ERROR_CODES(v0.29.0) — 추가만 하는 목록.
// 코어와의 대조(코드 집합)는 백엔드 apps/api/tests/test_web_node_error_codes_contract.py가 한다.
const CORE_CODES = [
  "rerank_unavailable",
  "gateway_error",
  "model_call_failed",
  "ssrf_blocked",
  "tool_unavailable",
  "usage_limit_exceeded",
  "context_length_exceeded",
  "llm_output_truncated",
];

describe("node-error-labels — 노드 실패 코드 사전 (core-v0.20.0-upgrade AC-2.3, §5.5)", () => {
  it("코어 노드 실패 코드 8종 모두에 문구가 있다", () => {
    expect([...KNOWN_NODE_ERROR_CODES].sort()).toEqual([...CORE_CODES].sort());
    for (const code of CORE_CODES) {
      expect(nodeErrorLabel(code)).not.toBe(NODE_ERROR_FALLBACK_LABEL);
    }
  });

  it.each([
    ["rerank_unavailable", "리랭커를 사용할 수 없어 검색을 진행하지 못했습니다"],
    ["model_call_failed", "모델 호출에 실패했습니다"],
    ["gateway_error", "모델 설정이나 입력을 확인해야 합니다"],
    ["ssrf_blocked", "내부 주소로의 요청이 차단되었습니다"],
    ["tool_unavailable", "도구를 사용할 수 없습니다"],
    ["usage_limit_exceeded", "사용량 한도를 넘었습니다"],
    ["context_length_exceeded", "입력이 모델 컨텍스트 한도를 넘었습니다"],
    ["llm_output_truncated", "모델 출력이 길이 상한에서 잘렸습니다"],
  ])("알려진 코드 %s → 계획 §5.5 문구", (code, label) => {
    expect(nodeErrorLabel(code)).toBe(label);
  });

  it.each([
    ["모르는 코드", "future_code"],
    ["null", null],
    ["undefined", undefined],
    ["빈 문자열", ""],
    ["대소문자가 다른 코드", "SSRF_BLOCKED"],
    ["Object 원형 속성", "constructor"],
    ["Object 원형 속성", "__proto__"],
    ["Object 원형 속성", "toString"],
  ])("%s(%s) → 일반 문구", (_label, code) => {
    expect(nodeErrorLabel(code)).toBe("노드 실행에 실패했습니다");
  });

  it("일반 문구 상수는 계획 §5.5 문구다", () => {
    expect(NODE_ERROR_FALLBACK_LABEL).toBe("노드 실행에 실패했습니다");
  });
});

describe("modelAttemptLabel — 시도한 모델의 실패 문구 (#216)", () => {
  it("circuit_open은 「연속 실패로 잠시 건너뛰었습니다」, 노드 실패 코드는 같은 문구, 모르는 코드는 일반 문구", () => {
    expect(modelAttemptLabel("circuit_open")).toBe("연속 실패로 잠시 건너뛰었습니다");
    expect(modelAttemptLabel("model_call_failed")).toBe("모델 호출에 실패했습니다");
    expect(modelAttemptLabel("context_length_exceeded")).toBe("입력이 모델 컨텍스트 한도를 넘었습니다");
    expect(modelAttemptLabel("future_code")).toBe(NODE_ERROR_FALLBACK_LABEL);
    expect(modelAttemptLabel(null)).toBe(NODE_ERROR_FALLBACK_LABEL);
  });

  it("circuit_open은 노드 실패 코드 사전에 넣지 않는다 — 코어 NODE_ERROR_CODES 대조가 그대로다", () => {
    expect(KNOWN_NODE_ERROR_CODES).not.toContain("circuit_open");
    expect(nodeErrorLabel("circuit_open")).toBe(NODE_ERROR_FALLBACK_LABEL);
  });
});
