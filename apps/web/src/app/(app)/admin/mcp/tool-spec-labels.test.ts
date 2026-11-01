/**
 * MCP 도구 노출 판정 문구 사전 (core-v0.20.0-upgrade AC-2.6, 계획 §5.7). 순수 모듈이라 mock 없이 입력 → 출력으로 본다.
 * 키 집합은 생성 타입 `Schemas["MCPToolSpecOut"]["reason"]`이 강제한다 — 아래 타입 단언이 `pnpm --filter web typecheck`
 * (`make ci`)에서 그 강제를 고정한다(코어가 사유를 더하면 생성 타입이 바뀌어 typecheck가 실패한다).
 */
import { describe, expect, expectTypeOf, it } from "vitest";
import {
  MCP_TOOL_APPROVAL_LABEL,
  MCP_TOOL_REJECT_REASON_LABEL,
  MCP_TOOL_SANITIZED_LABEL,
  mcpToolRejectedLabel,
  type MCPToolRejectReason,
} from "@/app/(app)/admin/mcp/tool-spec-labels";

describe("MCP 도구 노출 판정 문구 (AC-2.6, 계획 §5.7)", () => {
  it("거부 사유 타입은 코어 MCPToolSpecRejectReason 6종(생성 타입의 null 아닌 값)이다", () => {
    expectTypeOf<MCPToolRejectReason>().toEqualTypeOf<
      | "schema_not_object"
      | "schema_too_deep"
      | "schema_too_large"
      | "schema_structural_text"
      | "schema_unsupported_value"
      | "schema_expansion"
    >();
    // 사전은 그 타입으로 키가 강제된다 — 빠진 사유·생성 타입 밖 사유는 typecheck 오류다
    // @ts-expect-error 사유가 하나라도 빠지면 Record<MCPToolRejectReason, string>을 만족하지 못한다
    const missing: Record<MCPToolRejectReason, string> = { schema_not_object: "x" };
    const extra: typeof MCP_TOOL_REJECT_REASON_LABEL = {
      ...MCP_TOOL_REJECT_REASON_LABEL,
      // @ts-expect-error 생성 타입에 없는 사유는 사전 키가 될 수 없다
      schema_future_rule: "x",
    };
    expect(Object.keys(missing)).toHaveLength(1);
    expect(Object.keys(extra)).toHaveLength(7);
  });

  it("사유 라벨 6종은 계획 §5.7 문구(+ core-v0.22.0 schema_expansion) 그대로다", () => {
    expect(MCP_TOOL_REJECT_REASON_LABEL).toEqual({
      schema_not_object: "입력 스키마가 객체가 아닙니다",
      schema_too_deep: "입력 스키마가 너무 깊습니다",
      schema_too_large: "입력 스키마가 너무 큽니다",
      schema_structural_text: "입력 스키마에 허용되지 않는 글자가 있습니다",
      schema_unsupported_value: "입력 스키마에 지원하지 않는 값이 있습니다",
      schema_expansion: "입력 스키마를 펼치면 너무 큽니다",
    });
    expect(MCP_TOOL_SANITIZED_LABEL).toBe("설명 정화됨");
  });

  it("「노출 불가: <사유 라벨>」 — 모르는 사유는 원문, 사유가 없으면 「노출 불가」, 프로토타입 키는 새지 않는다", () => {
    for (const [reason, label] of Object.entries(MCP_TOOL_REJECT_REASON_LABEL)) {
      expect(mcpToolRejectedLabel(reason)).toBe(`노출 불가: ${label}`);
    }
    expect(mcpToolRejectedLabel("schema_future_rule")).toBe("노출 불가: schema_future_rule");
    expect(mcpToolRejectedLabel(null)).toBe("노출 불가");
    expect(mcpToolRejectedLabel(undefined)).toBe("노출 불가");
    expect(mcpToolRejectedLabel("")).toBe("노출 불가");
    expect(mcpToolRejectedLabel("constructor")).toBe("노출 불가: constructor");
  });
});

describe("MCP 도구 승인 고정 상태 문구 (core-v0.22.0-upgrade AC-3)", () => {
  it("상태 4종 — 생성 타입이 키를 강제하고 재승인이 필요한 둘은 그렇게 말한다", () => {
    expect(MCP_TOOL_APPROVAL_LABEL).toEqual({
      approved: "승인됨",
      unpinned: "미고정 — 재승인 필요",
      changed: "승인 뒤 변경됨 — 재승인 필요",
      not_approved: "승인 없음",
    });
  });
});
